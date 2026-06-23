"""把会话历史加工成一次模型请求需要的上下文。

学习思路：主流程是压缩历史、运行 ContextBuild hook、拼系统提示词，最后规范化为 API message。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from bigcode.config.models import CompactConfig
from bigcode.hooks.models import HookInput

from .attachments import Attachment
from .compact import CompactDeps, ContextCompactResult, ContextCompactState, apply_context_compact
from .messages import ApiMessage, AttachmentMessage, MessageBase
from .normalizer import normalize_messages_for_api


@dataclass
class ContextBuildDeps:
    """构建上下文时需要的外部依赖。

    用 dataclass 收集这些参数，是为了避免 build_context_for_api 的函数参数过长。
    """
    session_id: str
    cwd: object
    instruction_paths: list
    tool_names: list[str]
    hook_bus: object | None = None
    permission_mode: str = "default"
    plan_mode_state: object | None = None
    task_store: object | None = None
    task_list_id: str | None = None
    capabilities: list[str] = field(default_factory=list)
    system_prompt: str = ""
    compact_config: CompactConfig = field(default_factory=CompactConfig)
    compact_state: ContextCompactState = field(default_factory=ContextCompactState)
    context_window: int = 128000
    tool_schemas: list[dict] = field(default_factory=list)
    summary_callback: Callable[[str, str], Awaitable[str]] | None = None
    is_main_thread: bool = True
    protocol: str = "anthropic"


@dataclass
class ContextBuildResult:
    """上下文构建结果，包含 system_prompt、内部上下文消息、API 消息和压缩信息。"""
    system_prompt: str
    context_messages: list[MessageBase]
    api_messages: list[Any]
    compact_result: ContextCompactResult
    attachments: list[Attachment] = field(default_factory=list)


async def build_context_for_api(messages: list[MessageBase], deps: ContextBuildDeps) -> ContextBuildResult:
    """构建模型请求上下文的主入口。

    它先重建动态附件预算，再压缩历史，最后使用冻结的 system prompt 转换 API 消息。
    """
    attachments: list[Attachment] = []
    if deps.hook_bus:
        # ContextBuild hook 可以向模型上下文注入动态信息，例如当前任务列表、
        # Plan Mode 提醒、可用技能索引等。
        agg = await deps.hook_bus.emit(
            "ContextBuild",
            HookInput(
                hook_event_name="ContextBuild",
                session_id=deps.session_id,
                cwd=str(deps.cwd),
                permission_mode=deps.permission_mode,
                payload={
                    "message_count": len(messages),
                    "plan_mode_state": deps.plan_mode_state,
                    "turn_index": deps.compact_state.turn_index,
                    "step_index": deps.compact_state.step_index + 1,
                    "plan_file_exists": _plan_file_exists(deps.plan_mode_state),
                    "task_store": deps.task_store,
                    "task_list_id": deps.task_list_id,
                    "capabilities": deps.capabilities,
                },
            ),
        )
        attachments.extend(agg.attachments)

    attachment_messages = [AttachmentMessage(att) for att in attachments]
    if deps.hook_bus:
        await deps.hook_bus.emit(
            "PreCompact",
            HookInput(
                hook_event_name="PreCompact",
                session_id=deps.session_id,
                cwd=str(deps.cwd),
                permission_mode=deps.permission_mode,
            ),
        )
    compact_result = await apply_context_compact(
        messages,
        CompactDeps(
            config=deps.compact_config,
            state=deps.compact_state,
            context_window=deps.context_window,
            system_prompt=deps.system_prompt,
            tool_schemas=deps.tool_schemas,
            extra_context_messages=attachment_messages,
            is_main_thread=deps.is_main_thread,
            summarize=deps.summary_callback,
        ),
    )
    if deps.hook_bus and compact_result.records_to_append:
        await deps.hook_bus.emit(
            "PostCompact",
            HookInput(
                hook_event_name="PostCompact",
                session_id=deps.session_id,
                cwd=str(deps.cwd),
                permission_mode=deps.permission_mode,
                payload={"compact_result": compact_result},
            ),
        )

    # compact_result.projected_messages 是本次模型请求真正使用的消息历史；
    # 不一定等于 self.messages，因为可能已经被压缩过。
    context_messages = list(compact_result.projected_messages)
    context_messages.extend(attachment_messages)

    # 最后一步才把内部消息转 API 格式，这样前面的 hook/compact 都能使用内部结构。
    api_messages = normalize_messages_for_api(deps.system_prompt, context_messages, protocol=deps.protocol)
    return ContextBuildResult(deps.system_prompt, context_messages, api_messages, compact_result, attachments)


def _plan_file_exists(state: object | None) -> bool:
    plan_file = getattr(state, "plan_file", None)
    if not plan_file:
        return False
    try:
        from pathlib import Path

        return Path(str(plan_file)).exists()
    except OSError:
        return False
