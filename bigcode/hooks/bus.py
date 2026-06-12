"""HookBus 是事件分发器。

学习思路：AgentSession 和 ToolRunner 在关键节点 emit 事件，这里按优先级调用匹配的 HookHandler 并合并结果。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod

from bigcode.context.attachments import Attachment

from .models import HookAggregate, HookEvent, HookInput, HookOutput, HookResult, HookSource


class HookHandler(ABC):
    """Hook 处理器基类。

    子类声明自己关心的事件，并在 run() 中返回 HookOutput。
    """
    name: str
    source: HookSource = "built-in"
    events: tuple[HookEvent, ...] = ()
    priority: int = 100

    async def matches(self, input: HookInput) -> bool:
        """判断当前 handler 是否处理这个 HookInput 事件。"""
        return input.hook_event_name in self.events

    @abstractmethod
    async def run(self, input: HookInput) -> HookOutput:
        """HookHandler 子类必须实现的执行入口。"""
        raise NotImplementedError


class HookBus:
    """Hook 事件总线。

    它保存所有 handler，emit 时按 priority 顺序调用并合并输出。
    """
    def __init__(self) -> None:
        """初始化空的 handler 列表。"""
        self._handlers: list[HookHandler] = []

    def register(self, handler: HookHandler) -> None:
        """注册一个 HookHandler，并按 priority 从小到大排序。"""
        self._handlers.append(handler)
        self._handlers.sort(key=lambda h: h.priority)

    async def emit(self, event: HookEvent, input: HookInput) -> HookAggregate:
        """触发一个 hook 事件。

        它会依次运行匹配 handler，收集结果，并合并 decision、attachments、updated_input 等字段。
        """
        input.hook_event_name = event
        results: list[HookResult] = []

        # 下面这些变量是“多个 hook 运行结果合并后的当前状态”。
        # 循环里每运行一个 handler，就可能更新它们。
        decision = "passthrough"
        reason = ""
        updated = input.payload.get("tool_input") if event == "PreToolUse" else None
        attachments: list[Attachment] = []
        continue_turn = False
        stop_reason = None

        # 使用 list(self._handlers) 是为了遍历时拿一个快照。
        # 即使某个 hook 在运行中注册/移除 handler，也不会影响本次 emit。
        for handler in list(self._handlers):
            if not await handler.matches(input):
                continue
            started = time.perf_counter()
            try:
                output = await handler.run(input)
                error = None
            except Exception as exc:
                # Hook 失败不应该让主流程崩溃；失败会被转成 Attachment，
                # 这样模型或用户能看到 hook 错误，但会话还能继续。
                output = HookOutput(
                    attachments=[Attachment(type="hook_execution_error", text=f"{handler.name} failed: {exc}", source="hooks")]
                )
                error = str(exc)
            duration = int((time.perf_counter() - started) * 1000)
            results.append(HookResult(event=event, source=handler.source, name=handler.name, output=output, duration_ms=duration, error=error))
            if output.updated_input is not None and event == "PreToolUse":
                # 多个 PreToolUse hook 串行执行。前一个 hook 改过的输入，
                # 会写回 payload，后一个 hook 看到的是更新后的工具输入。
                updated = output.updated_input
                input.payload["tool_input"] = updated
            if output.additional_context:
                # additional_context 是字符串快捷方式，这里统一包装成 Attachment。
                attachments.append(Attachment(type="hook_context", text=output.additional_context, source=handler.name))
            attachments.extend(output.attachments)

            # decision 合并规则故意保守：
            # block 优先级最高，ask 次之，approve 只能在还没有其它决策时生效。
            if output.decision == "block":
                decision = "block"
                reason = output.reason
            elif output.decision == "ask" and decision != "block":
                decision = "ask"
                reason = output.reason
            elif output.decision == "approve" and decision == "passthrough":
                decision = "approve"
                reason = output.reason
            if output.continue_turn:
                continue_turn = True
            if output.stop_reason:
                stop_reason = output.stop_reason
        return HookAggregate(
            event=event,
            results=results,
            decision=decision,
            reason=reason,
            updated_input=updated if event == "PreToolUse" else None,
            attachments=attachments,
            continue_turn=continue_turn,
            stop_reason=stop_reason,
        )
