"""工具执行调度器。

学习思路：模型返回 tool_use 后会进入这里，按校验、hook、权限、执行、截断、记录元数据的顺序处理。
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from pydantic import ValidationError

from bigcode.hooks.models import HookInput

from .base import PermissionDecision, ToolExecutionContext, ToolRunResult
from .artifacts import serialized_chars
from .output_limits import limit_tool_run_result
from .permissions import decide_permission
from .registry import ToolRegistry


@dataclass(frozen=True)
class ToolUse:
    """模型返回的单个工具调用请求。

    id 用来和 tool_result 对应；name 是工具名；input 是模型给出的原始参数。
    """
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class _ShellCommandParts:
    """审批摘要使用的 shell 分段结果。"""

    segments: list[str]
    separators: list[str]
    is_complex: bool = False


class ToolRunner:
    """统一执行工具调用的调度器。

    它负责并发安全判断、输入校验、hook、权限询问、真正调用工具、截断大结果和事件上报。
    """
    def __init__(self, registry: ToolRegistry) -> None:
        """保存工具注册表，后续执行工具时按 name 从这里查找实现。"""
        self.registry = registry

    async def run_tool_uses(self, tool_uses: list[ToolUse], ctx: ToolExecutionContext) -> AsyncIterator[Any]:
        """按顺序执行一批 tool_use，并把安全的只读工具合并并发运行。

        会修改状态的工具必须等前面的安全批次完成后再单独执行。
        """
        self._ensure_registry_context(ctx)
        safe_batch: list[tuple[int, ToolUse]] = []

        async def flush_safe_batch() -> AsyncIterator[Any]:
            """执行当前累积的并发安全工具批次，并把结果放回原始顺序的位置。"""
            nonlocal safe_batch
            if not safe_batch:
                return
            batch = safe_batch
            safe_batch = []
            queues: list[asyncio.Queue[Any]] = [asyncio.Queue() for _ in batch]

            async def pump(queue: asyncio.Queue[Any], tool_use: ToolUse) -> None:
                try:
                    async for event in self.run_one_stream(tool_use, ctx):
                        await queue.put(event)
                finally:
                    await queue.put(None)

            tasks = [asyncio.create_task(pump(queue, tool_use)) for queue, (_, tool_use) in zip(queues, batch)]
            active = set(range(len(queues)))
            ordered_results: list[tuple[int, ToolRunResult[Any]]] = []
            try:
                while active:
                    getters = {asyncio.create_task(queues[i].get()): i for i in active}
                    done, pending = await asyncio.wait(getters, return_when=asyncio.FIRST_COMPLETED)
                    for pending_task in pending:
                        pending_task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for done_task in done:
                        queue_index = getters[done_task]
                        event = done_task.result()
                        if event is None:
                            active.discard(queue_index)
                            continue
                        if isinstance(event, ToolRunResult):
                            ordered_results.append((batch[queue_index][0], event))
                            continue
                        yield event
                await asyncio.gather(*tasks)
                for _, result in sorted(ordered_results, key=lambda item: item[0]):
                    yield result
            except Exception:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

        for idx, tool_use in enumerate(tool_uses):
            if ctx.abort_event.is_set():
                async for event in flush_safe_batch():
                    yield event
                for event in self._aborted_result(tool_use, ctx):
                    yield event
                continue

            # 只读、无状态副作用的工具可以暂存到 safe_batch 里并发执行。
            # 一旦遇到可能改状态的工具，就先 flush，保持模型给出的工具顺序语义。
            if self._is_concurrency_safe(tool_use, ctx):
                safe_batch.append((idx, tool_use))
                continue
            async for event in flush_safe_batch():
                yield event
            async for event in self.run_one_stream(tool_use, ctx):
                yield event
        async for event in flush_safe_batch():
            yield event

    async def run_one(self, tool_use: ToolUse, ctx: ToolExecutionContext) -> ToolRunResult[Any]:
        """执行单个工具并返回最终 ToolRunResult。

        流式事件消费请使用 run_one_stream()；这个兼容入口用于测试和只关心最终结果的调用方。
        """
        result: ToolRunResult[Any] | None = None
        async for event in self.run_one_stream(tool_use, ctx):
            if isinstance(event, ToolRunResult):
                result = event
        if result is None:
            return ToolRunResult(tool_use.id, tool_use.name, is_error=True, error_message="Tool execution did not produce a result.")
        return result

    async def run_one_stream(self, tool_use: ToolUse, ctx: ToolExecutionContext) -> AsyncIterator[Any]:
        """执行单个工具调用的完整流程。

        阅读这个方法能看到工具系统的主链路：找工具、Pydantic 校验、hook、权限、call、截断和 PostToolUse。
        """
        self._ensure_registry_context(ctx)
        started = time.perf_counter()
        from bigcode.agent.events import ToolStarted

        yield ToolStarted(session_id=ctx.session_id, tool_use_id=tool_use.id, tool_name=tool_use.name)

        # 第一步：按工具名找到实现。模型可能幻觉出不存在的工具名，
        # 所以这里必须把未知工具转换成普通 ToolRunResult 错误。
        tool = self.registry.get(tool_use.name)
        if tool is None:
            for event in self._finish(
                tool_use,
                ctx,
                started,
                tool_use.name,
                ToolRunResult(
                    tool_use.id,
                    tool_use.name,
                    is_error=True,
                    error_message=f"Unknown tool: {tool_use.name}",
                    metadata={"unknown_tool": True},
                ),
            ):
                yield event
            yield ToolRunResult(
                tool_use.id,
                tool_use.name,
                is_error=True,
                error_message=f"Unknown tool: {tool_use.name}",
                metadata={"unknown_tool": True},
            )
            return
        try:
            # 第二步：用工具自己的 Pydantic input_model 校验参数。
            # 校验后得到的是强类型对象，后面的权限和 call 都使用它。
            input_model = tool.input_model.model_validate(tool_use.input)
        except ValidationError as exc:
            run_result = ToolRunResult(tool_use.id, tool.name, is_error=True, error_message=f"Invalid input: {exc}")
            for event in self._finish(
                tool_use,
                ctx,
                started,
                tool.name,
                run_result,
            ):
                yield event
            yield run_result
            return
        if not tool.is_enabled(ctx):
            run_result = ToolRunResult(tool_use.id, tool.name, is_error=True, error_message=f"Tool {tool.name} is disabled.")
            for event in self._finish(
                tool_use,
                ctx,
                started,
                tool.name,
                run_result,
            ):
                yield event
            yield run_result
            return
        validation = await tool.validate_input(input_model, ctx)
        if not validation.ok:
            run_result = ToolRunResult(tool_use.id, tool.name, is_error=True, error_message=validation.message)
            for event in self._finish(
                tool_use,
                ctx,
                started,
                tool.name,
                run_result,
            ):
                yield event
            yield run_result
            return

        # 第三步：统一权限系统收敛成 allow / ask / deny。
        perm = await decide_permission(tool, input_model, ctx)
        if perm.behavior == "deny":
            run_result = ToolRunResult(tool_use.id, tool.name, is_error=True, error_message=perm.message or "Permission denied.")
            for event in self._finish(
                tool_use,
                ctx,
                started,
                tool.name,
                run_result,
            ):
                yield event
            yield run_result
            return
        if perm.behavior == "ask":
            from bigcode.agent.events import PermissionRequested, PermissionResolved

            approval_lines = _permission_approval_lines(tool.name, input_model.model_dump(), ctx)
            summary = approval_lines[0] if len(approval_lines) == 1 else _permission_summary(tool.name, input_model.model_dump(), ctx)
            yield PermissionRequested(
                session_id=ctx.session_id,
                tool_use_id=tool_use.id,
                tool_name=tool.name,
                summary=summary,
                metadata={
                    "message": perm.message,
                    "reason": perm.reason,
                    "decision_reason": perm.decision_reason,
                    "approval_lines": approval_lines,
                },
            )
            approved, source = await self._ask_permission(tool, input_model, perm, ctx, approval_lines=approval_lines)
            yield PermissionResolved(
                session_id=ctx.session_id,
                tool_use_id=tool_use.id,
                tool_name=tool.name,
                approved=approved,
                source=source,
            )
            if not approved:
                run_result = ToolRunResult(tool_use.id, tool.name, is_error=True, error_message="permission denied by user.")
                for event in self._finish(
                    tool_use,
                    ctx,
                    started,
                    tool.name,
                    run_result,
                ):
                    yield event
                yield run_result
                return

        try:
            # 第四步：真正执行工具。工具内部抛异常也会被包装成 ToolRunResult，
            # 这样模型能收到失败原因，而不是让整个 session 崩掉。
            def on_progress(progress: Any) -> None:
                from bigcode.agent.events import ToolProgress

                progress_events.append(
                    ToolProgress(
                        session_id=ctx.session_id,
                        tool_use_id=tool_use.id,
                        tool_name=tool.name,
                        progress=progress,
                    )
                )

            progress_events: list[Any] = []
            result = await tool.call(input_model, ctx, on_progress=on_progress)
            for progress_event in progress_events:
                yield progress_event
            run_result = ToolRunResult(tool_use.id, tool.name, output=result)
        except Exception as exc:
            run_result = ToolRunResult(tool_use.id, tool.name, is_error=True, error_message=str(exc))

        # 第五步：大结果先落盘成 artifact，再把上下文里的结果裁剪到安全大小。
        self._offload_large_result(run_result, tool.max_result_chars, ctx)
        run_result = limit_tool_run_result(run_result, tool.max_result_chars)
        self._record_metadata_carryover(tool.name, input_model.model_dump(), run_result, ctx)
        if ctx.hook_bus:
            # PostToolUse hook 只能观察执行结果；当前实现不使用它修改 run_result。
            await ctx.hook_bus.emit(
                "PostToolUse",
                HookInput(
                    hook_event_name="PostToolUse",
                    session_id=ctx.session_id,
                    cwd=str(ctx.cwd),
                    permission_mode=ctx.permission_context.mode,
                    payload={
                        "tool_name": tool.name,
                        "tool_input": input_model.model_dump(),
                        "tool_use_id": tool_use.id,
                        "tool_result": run_result.output.data if run_result.output else None,
                        "is_error": run_result.is_error,
                    },
                ),
            )
        for event in self._finish(tool_use, ctx, started, tool.name, run_result):
            yield event
        yield run_result

    def _aborted_result(self, tool_use: ToolUse, ctx: ToolExecutionContext) -> list[Any]:
        """当会话收到 abort 信号时，为还没运行的工具生成一个统一的错误结果。"""
        started = time.perf_counter()
        from bigcode.agent.events import ToolStarted

        return [
            ToolStarted(session_id=ctx.session_id, tool_use_id=tool_use.id, tool_name=tool_use.name),
            *self._finish(
                tool_use,
                ctx,
                started,
                tool_use.name,
                ToolRunResult(tool_use.id, tool_use.name, is_error=True, error_message="Aborted."),
            ),
            ToolRunResult(tool_use.id, tool_use.name, is_error=True, error_message="Aborted."),
        ]

    def _finish(
        self,
        tool_use: ToolUse,
        ctx: ToolExecutionContext,
        started: float,
        tool_name: str,
        result: ToolRunResult[Any],
    ) -> list[Any]:
        """工具执行结束的统一收口点。

        它补充耗时事件，错误时额外发送 ErrorEvent，然后返回 ToolRunResult。
        """
        duration_ms = int((time.perf_counter() - started) * 1000)
        from bigcode.agent.events import ErrorEvent, ToolCompleted

        events: list[Any] = [
            ToolCompleted(
                session_id=ctx.session_id,
                tool_use_id=tool_use.id,
                tool_name=tool_name,
                is_error=result.is_error,
                duration_ms=duration_ms,
                metadata={"truncated": bool(result.metadata.get("truncated"))},
            )
        ]
        if result.is_error:
            events.append(
                ErrorEvent(
                    session_id=ctx.session_id,
                    tool_use_id=tool_use.id,
                    tool_name=tool_name,
                    message=result.error_message or "Tool execution failed.",
                )
            )
        return events

    async def _ask_permission(
        self,
        tool: Any,
        input_model: Any,
        decision: PermissionDecision,
        ctx: ToolExecutionContext,
        *,
        approval_lines: list[str] | None = None,
    ) -> tuple[bool, str]:
        """统一处理权限 ask。"""
        if ctx.permission_context.mode == "dontAsk" or ctx.permission_context.should_avoid_permission_prompts:
            return False, "mode"
        if ctx.is_non_interactive_session:
            if ctx.hook_bus:
                agg = await ctx.hook_bus.emit(
                    "PermissionRequest",
                    HookInput(
                        hook_event_name="PermissionRequest",
                        session_id=ctx.session_id,
                        cwd=str(ctx.cwd),
                        permission_mode=ctx.permission_context.mode,
                        payload={
                            "tool_name": tool.name,
                            "tool_input": input_model.model_dump(),
                            "permission_decision": {
                                "behavior": decision.behavior,
                                "message": decision.message,
                                "reason": decision.reason,
                                "rule": decision.rule,
                                "decision_reason": decision.decision_reason,
                            },
                        },
                    ),
                )
                if agg.decision == "approve":
                    return True, "hook"
                if agg.decision == "block":
                    return False, "hook"
            return False, "non_interactive"
        lines = approval_lines or _permission_approval_lines(tool.name, input_model.model_dump(), ctx)
        for line in lines:
            approved = await _approve_line(line, ctx)
            if not approved:
                return False, "user"
        return True, "user"

    def _is_concurrency_safe(self, tool_use: ToolUse, ctx: ToolExecutionContext) -> bool:
        """判断某个工具调用能否和其它工具并发运行。

        当前并发语义跟随工具自己的只读判断。
        """
        tool = self.registry.get(tool_use.name)
        if tool is None:
            return False
        try:
            input_model = tool.input_model.model_validate(tool_use.input)
        except ValidationError:
            return False
        return tool.is_read_only(input_model, ctx)

    def _ensure_registry_context(self, ctx: ToolExecutionContext) -> None:
        """让工具调用能访问当前 registry，例如 Tool_Search 标记 discovered 工具。"""
        if ctx.tool_registry is None:
            ctx.tool_registry = self.registry

    def _offload_large_result(self, result: ToolRunResult[Any], max_chars: int, ctx: ToolExecutionContext) -> None:
        """把超大工具结果写入 artifact 文件，并把 artifact 元数据塞回结果里。"""
        if result.output is None or ctx.artifact_store is None:
            return
        original_chars = serialized_chars(result.output.data)
        if original_chars <= max_chars:
            return

        record = ctx.artifact_store.write_tool_output(
            tool_use_id=result.tool_use_id,
            tool_name=result.tool_name,
            output=result.output.data,
            output_metadata=result.output.metadata,
            run_metadata=result.metadata,
            is_error=result.is_error,
            error_message=result.error_message,
        )
        metadata = {
            "artifact_path": record.artifact_path,
            "original_chars": record.original_chars,
        }

        # metadata 同时放在 run_result 和 output.metadata 两处，是为了兼容不同读取路径：
        # normalizer 会合并两边 metadata，event/UI 层也可能直接看 run_result.metadata。
        result.metadata = {**result.metadata, **metadata}
        result.output.metadata = {**result.output.metadata, **metadata}
        if result.tool_name == "Read" and isinstance(result.output.data, dict):
            file_path = result.output.data.get("file_path")
            if isinstance(file_path, str):
                ctx.read_file_state.mark_partial_view(Path(file_path))

    def _record_metadata_carryover(self, tool_name: str, tool_input: dict[str, Any], result: ToolRunResult[Any], ctx: ToolExecutionContext) -> None:
        """把工具结果带来的会话状态变化同步回 AgentSession。

        例如 SkillLoad 会登记已加载技能，测试类 Bash 命令会登记 last_verification。
        """
        if not ctx.agent_session:
            return
        if tool_name == "SkillLoad" and not result.is_error:
            name = tool_input.get("name")
            if isinstance(name, str) and hasattr(ctx.agent_session, "record_loaded_skill"):
                ctx.agent_session.record_loaded_skill(name)
        if tool_name == "Bash" and hasattr(ctx.agent_session, "record_last_verification"):
            command = tool_input.get("command")
            if isinstance(command, str) and _is_verification_command(command):
                exit_code = None
                if result.output and isinstance(result.output.data, dict):
                    value = result.output.data.get("exit_code")
                    exit_code = value if isinstance(value, int) else None
                ctx.agent_session.record_last_verification(command=command, exit_code=exit_code)


async def _approve_line(line: str, ctx: ToolExecutionContext) -> bool:
    """审批单行动作；交互式 UI 可通过 context 注入 transient prompt。"""
    if ctx.approval_cache is not None and line in ctx.approval_cache:
        return ctx.approval_cache[line]
    if ctx.approval_callback is not None:
        approved = await ctx.approval_callback(line)
    else:
        approved = await asyncio.to_thread(_read_yes_no, f"{line} [y/N] ")
    if ctx.approval_cache is not None and approved:
        ctx.approval_cache[line] = approved
    return approved


def _read_yes_no(prompt: str) -> bool:
    """读取明确 yes/no 的确认输入；EOF 或空回车按拒绝处理。"""
    from bigcode.ui.prompt import INVALID_APPROVAL_PROMPT, parse_yes_no

    while True:
        try:
            value = input(prompt)
        except EOFError:
            return False
        parsed = parse_yes_no(value)
        if parsed is not None:
            return parsed
        prompt = INVALID_APPROVAL_PROMPT


_SENSITIVE_FIELD_RE = re.compile(r"(api[_-]?key|token|password|secret|authorization|credential)", re.IGNORECASE)
_MAX_INLINE_CHARS = 180


def _format_permission_prompt(tool_name: str, message: str, input_model: Any, ctx: ToolExecutionContext) -> str:
    """生成给用户看的单行审批提示。

    权限决策本身仍然在 permissions.py 中完成；这里只把工具调用翻译成用户能快速判断的动作摘要。
    """
    data = input_model.model_dump() if hasattr(input_model, "model_dump") else {}
    summary = _permission_summary(tool_name, data, ctx)
    if message:
        return f"\n{summary}\n{message}\nApprove? Type yes/y to allow, no/n or Enter to deny: "
    return f"\n{summary}\nApprove? Type yes/y to allow, no/n or Enter to deny: "


def _permission_approval_lines(tool_name: str, data: dict[str, Any], ctx: ToolExecutionContext) -> list[str]:
    """生成逐条、单行审批提示。"""
    if tool_name == "Bash":
        command = str(data.get("command") or "").strip()
        parsed = _split_shell_command(command)
        if command and not parsed.is_complex and parsed.segments:
            return [f"Approve Bash: {_simple_bash_action(segment, ctx)} ?" for segment in parsed.segments]
        return [f"Approve Bash: {_inline_preview(command or '(empty)')} ?"]
    if tool_name == "Write":
        return [f"Approve Write: {_short_path(data.get('file_path'), ctx)} ?"]
    if tool_name == "Edit":
        return [f"Approve Edit: {_short_path(data.get('file_path'), ctx)} ?"]
    if tool_name == "WebFetch":
        return [f"Approve WebFetch: {_inline_preview(data.get('url') or '(missing)')} ?"]
    if tool_name == "WebSearch":
        return [f"Approve WebSearch: {_inline_preview(data.get('query') or '(empty)')} ?"]
    if tool_name == "Agent":
        subagent_type = data.get("subagent_type") or "general-purpose"
        return [f"Approve Agent: {subagent_type} ?"]
    path_value = data.get("file_path") or data.get("path")
    if path_value:
        return [f"Approve {tool_name}: {_short_path(path_value, ctx)} ?"]
    if data.get("command"):
        return [f"Approve {tool_name}: {_inline_preview(data['command'])} ?"]
    return [f"Approve {tool_name} ?"]


def _permission_summary(tool_name: str, data: dict[str, Any], ctx: ToolExecutionContext) -> str:
    """按工具类型生成一句话动作摘要。"""
    if tool_name == "Bash":
        return _bash_permission_summary(str(data.get("command") or ""), ctx)

    if tool_name == "Write":
        content = str(data.get("content") or "")
        return f"需要写入文件：{_short_path(data.get('file_path'), ctx)}（{len(content)} 字符，{_line_count(content)} 行）"

    if tool_name == "Edit":
        scope = "全部匹配" if data.get("replace_all") else "首个匹配"
        return f"需要修改文件：{_short_path(data.get('file_path'), ctx)}（替换{scope}）"

    if tool_name == "WebFetch":
        return f"需要访问网页：{_inline_preview(data.get('url') or '(missing)')}"

    if tool_name == "WebSearch":
        return f"需要联网搜索：{_inline_preview(data.get('query') or '(empty)')}"

    if tool_name.startswith("External"):
        return _external_permission_summary(tool_name, data)

    if tool_name == "Agent":
        subagent_type = data.get("subagent_type") or "general-purpose"
        background = bool(data.get("background") or data.get("run_in_background"))
        description = data.get("description") or data.get("prompt") or ""
        mode = "后台" if background else "同步"
        detail = f"：{_inline_preview(description)}" if description else ""
        return f"需要{mode}启动子代理 {subagent_type}{detail}"

    path_value = data.get("file_path") or data.get("path")
    if path_value:
        return f"需要使用 {tool_name} 处理路径：{_short_path(path_value, ctx)}"
    if data.get("url"):
        return f"需要使用 {tool_name} 访问：{_inline_preview(data['url'])}"
    if data.get("command"):
        return f"需要使用 {tool_name} 执行：{_inline_preview(data['command'])}"
    if data:
        return f"需要使用 {tool_name}：{_safe_inline_json(data)}"
    return f"需要使用 {tool_name}"


def _bash_permission_summary(command: str, ctx: ToolExecutionContext) -> str:
    """把常见 shell 命令翻译成更具体的动作。"""
    command = command.strip()
    if not command:
        return "需要执行空命令"
    parsed = _split_shell_command(command)
    if parsed.is_complex:
        return f"需要执行复杂 shell 命令：{_inline_preview(command)}"
    if len(parsed.segments) > 1:
        actions = [_simple_bash_action(segment, ctx) for segment in parsed.segments]
        shown = "；".join(actions[:2])
        if len(actions) > 2:
            shown = f"{shown}；等 {len(actions)} 条命令"
        return f"需要执行 {len(actions)} 条 shell 命令：{shown}"
    if not parsed.segments:
        return "需要执行空命令"
    return f"需要{_simple_bash_action(parsed.segments[0], ctx)}"


def _simple_bash_action(segment: str, ctx: ToolExecutionContext) -> str:
    """把一段不含 shell 控制符的简单命令翻译成动作。"""
    try:
        parts = shlex.split(segment)
    except ValueError:
        return f"执行命令：{_inline_preview(segment)}"
    if not parts:
        return "执行空命令"
    program = Path(parts[0]).name
    targets = _shell_targets(parts[1:])
    if program == "rm" and targets:
        return f"删除{_target_count_label(targets)}：{_format_targets(targets, ctx)}"
    if program == "mkdir" and targets:
        return f"创建目录：{_format_targets(targets, ctx)}"
    if program == "touch" and targets:
        return f"创建或更新时间戳：{_format_targets(targets, ctx)}"
    if program == "mv" and len(targets) >= 2:
        return f"移动/重命名：{_short_shell_path(targets[0], ctx)} -> {_short_shell_path(targets[-1], ctx)}"
    if program == "cp" and len(targets) >= 2:
        return f"复制：{_short_shell_path(targets[0], ctx)} -> {_short_shell_path(targets[-1], ctx)}"
    if program == "chmod" and targets:
        return f"修改权限：{_format_targets(targets[1:] or targets, ctx)}"
    if program == "chown" and targets:
        return f"修改所有者：{_format_targets(targets[1:] or targets, ctx)}"
    if program == "ls":
        ls_targets = _shell_targets(parts[1:], options_with_values={"--sort", "--time", "--format"})
        return f"查看目录：{_format_targets(ls_targets, ctx) if ls_targets else _short_path('.', ctx)}"
    if program == "cat":
        cat_targets = _shell_targets(parts[1:])
        if cat_targets:
            return f"查看文件：{_format_targets(cat_targets, ctx)}"
    if program == "head":
        head_targets = _shell_targets(parts[1:], options_with_values={"-n", "-c", "--lines", "--bytes"})
        if head_targets:
            return f"查看文件开头：{_format_targets(head_targets, ctx)}"
    if program == "tail":
        tail_targets = _shell_targets(parts[1:], options_with_values={"-n", "-c", "--lines", "--bytes"})
        if tail_targets:
            return f"查看文件末尾：{_format_targets(tail_targets, ctx)}"
    if program in {"grep", "rg"}:
        return f"搜索文本：{_inline_preview(segment)}"
    if program == "find":
        return f"查找文件：{_inline_preview(segment)}"
    if program == "git":
        return _git_bash_action(parts, segment)
    return f"执行命令：{_inline_preview(segment)}"


def _split_shell_command(command: str) -> _ShellCommandParts:
    """按 shell 控制符分段，同时识别不适合推断的复杂语法。

    这不是完整 shell 解析器；它只服务审批摘要。遇到容易误判的语法时返回 is_complex。
    """
    segments: list[str] = []
    separators: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    is_complex = False
    i = 0
    while i < len(command):
        ch = command[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\" and quote != "'":
            buf.append(ch)
            escaped = True
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            i += 1
            continue
        two = command[i : i + 2]
        if two in {"&&", "||"}:
            _flush_shell_segment(buf, segments)
            separators.append(two)
            i += 2
            continue
        if ch in {";", "\n", "|"}:
            _flush_shell_segment(buf, segments)
            separators.append(ch)
            if ch == "|":
                is_complex = True
            i += 1
            continue
        if ch in {">", "<", "`", "*", "?"}:
            is_complex = True
        if ch == "$" and command[i : i + 2] == "$(":
            is_complex = True
        buf.append(ch)
        i += 1
    if quote or escaped:
        is_complex = True
    _flush_shell_segment(buf, segments)
    return _ShellCommandParts([segment for segment in segments if segment], separators, is_complex)


def _flush_shell_segment(buf: list[str], segments: list[str]) -> None:
    """把当前字符缓冲区压成一个命令段。"""
    segment = "".join(buf).strip()
    if segment:
        segments.append(segment)
    buf.clear()


def _git_bash_action(parts: list[str], segment: str) -> str:
    """给常见 Git 子命令生成更明确的摘要。"""
    if len(parts) < 2:
        return "执行 Git 命令：git"
    sub = parts[1]
    if sub == "status":
        return "查看 Git 状态"
    if sub == "diff":
        return f"查看 Git 差异：{_inline_preview(segment)}"
    if sub in {"log", "show"}:
        return f"查看 Git 历史：{_inline_preview(segment)}"
    if sub in {"grep", "ls-files", "rev-parse", "branch"}:
        return f"查看 Git 信息：{_inline_preview(segment)}"
    return f"执行 Git 命令：{_inline_preview(segment)}"


def _external_permission_summary(tool_name: str, data: dict[str, Any]) -> str:
    """把 MCP 工具调用压缩成一句话。"""
    server = data.get("server")
    if tool_name == "ExternalResourceList":
        suffix = f"（server: {server}）" if server else ""
        return f"需要列出外部 MCP 资源{suffix}"
    if tool_name == "ExternalResourceRead":
        return f"需要读取外部 MCP 资源：{_inline_preview(data.get('uri') or '(missing)')}"
    if tool_name == "ExternalPromptList":
        suffix = f"（server: {server}）" if server else ""
        return f"需要列出外部 MCP Prompt{suffix}"
    if tool_name == "ExternalPromptGet":
        return f"需要获取外部 MCP Prompt：{_inline_preview(data.get('name') or '(missing)')}"
    return f"需要使用 {tool_name}"


def _shell_targets(parts: list[str], *, options_with_values: set[str] | None = None) -> list[str]:
    """从简单 shell 参数中取出非 option 的目标路径。"""
    options_with_values = options_with_values or set()
    targets: list[str] = []
    option_mode = True
    skip_next = False
    for part in parts:
        if skip_next:
            skip_next = False
            continue
        if option_mode and part == "--":
            option_mode = False
            continue
        if option_mode and part.startswith("-"):
            option_name = part.split("=", 1)[0]
            if option_name in options_with_values and "=" not in part:
                skip_next = True
            continue
        targets.append(part)
    return targets


def _target_count_label(targets: list[str]) -> str:
    """根据目标数量生成中文数量短语。"""
    return "文件/路径" if len(targets) == 1 else f" {len(targets)} 个文件/路径"


def _format_targets(targets: list[str], ctx: ToolExecutionContext) -> str:
    """展示一个或多个 shell 目标，最多展开三个。"""
    shown = [_short_shell_path(target, ctx) for target in targets[:3]]
    suffix = f" 等 {len(targets)} 项" if len(targets) > 3 else ""
    return _inline_preview(", ".join(shown) + suffix)


def _short_shell_path(value: Any, ctx: ToolExecutionContext) -> str:
    """shell 参数可能包含变量或通配符，这类参数保持原样。"""
    text = str(value)
    if any(marker in text for marker in ("*", "?", "$", "`", "~")):
        return _inline_preview(text, max_chars=120)
    return _short_path(text, ctx)


def _short_path(value: Any, ctx: ToolExecutionContext) -> str:
    """路径展示统一走绝对路径，并限制长度。"""
    return _inline_preview(_display_path(value, ctx), max_chars=140)


def _display_path(value: Any, ctx: ToolExecutionContext) -> str:
    """把相对路径展示成基于 cwd 的绝对路径，方便用户确认影响范围。"""
    if value is None:
        return "(missing)"
    path = str(value)
    try:
        candidate = ctx.cwd / path
        if not candidate.is_absolute():
            candidate = ctx.cwd / path
        return str(candidate.resolve(strict=False))
    except Exception:
        return path


def _line_count(text: str) -> int:
    """统计适合展示给用户的文本行数。"""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _inline_preview(value: Any, *, max_chars: int = _MAX_INLINE_CHARS) -> str:
    """把任意文本压成一行短摘要。"""
    text = _redact_inline(str(value))
    text = " ".join(text.split())
    if not text:
        return "(empty)"
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _safe_inline_json(value: Any) -> str:
    """把兜底工具输入转成单行 JSON 摘要。"""
    safe_value = _sanitize_for_prompt(value)
    return _inline_preview(json.dumps(safe_value, ensure_ascii=False, sort_keys=True))


def _sanitize_for_prompt(value: Any, *, key: str = "") -> Any:
    """递归清理要展示的工具输入。"""
    if key and _SENSITIVE_FIELD_RE.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _sanitize_for_prompt(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_for_prompt(item) for item in value[:20]]
    if isinstance(value, tuple):
        return [_sanitize_for_prompt(item) for item in value[:20]]
    if isinstance(value, str):
        return _inline_preview(value, max_chars=120)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _inline_preview(str(value), max_chars=120)


def _redact_inline(text: str) -> str:
    """尽量隐藏命令或普通字符串里常见的明文密钥写法。"""
    return re.sub(r"(?i)(api[_-]?key|token|password|secret|authorization)=([^\s]+)", r"\1=<redacted>", text)


def _is_verification_command(command: str) -> bool:
    """粗略判断 Bash 命令是否是测试、构建或 lint，用于记录最近验证命令。"""
    lowered = command.strip().lower()
    if not lowered:
        return False
    try:
        parts = shlex.split(lowered)
    except ValueError:
        parts = lowered.split()
    if not parts:
        return False
    joined = " ".join(parts)
    first = parts[0]
    if first in {"pytest", "mypy", "ruff"}:
        return True
    if first in {"cargo", "npm", "pnpm", "yarn"} and any(part in {"test", "build", "lint"} for part in parts[1:]):
        return True
    if first in {"python", "python3"}:
        return any(part in {"pytest", "unittest", "mypy"} or part.endswith("pytest") for part in parts[1:])
    if joined.startswith(("test", "lint", "build", "unittest")):
        return True
    return any(keyword in joined for keyword in (" pytest", " unittest", " lint", " mypy", " ruff", " test", " build"))
