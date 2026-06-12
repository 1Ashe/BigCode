"""把会话历史加工成一次模型请求需要的上下文。

学习思路：主流程是压缩历史、运行 ContextBuild hook、拼系统提示词，最后规范化为 API message。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from bigcode.hooks.models import HookInput

from .attachments import Attachment
from .compact import ContextCompactResult, apply_context_compact
from .messages import ApiMessage, MessageBase
from .normalizer import attachment_to_user_message, normalize_messages_for_api
from .system_prompt import build_system_prompt


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


@dataclass
class ContextBuildResult:
    """上下文构建结果，包含 system_prompt、内部上下文消息、API 消息和压缩信息。"""
    system_prompt: str
    context_messages: list[MessageBase]
    api_messages: list[ApiMessage]
    compact_result: ContextCompactResult
    attachments: list[Attachment] = field(default_factory=list)


async def build_context_for_api(messages: list[MessageBase], deps: ContextBuildDeps) -> ContextBuildResult:
    """构建模型请求上下文的主入口。

    它先压缩历史，再运行 ContextBuild hook 注入附件，最后组装 system prompt 并转换 API 消息。
    """
    compact_result = await apply_context_compact(messages)
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
                    "task_store": deps.task_store,
                    "task_list_id": deps.task_list_id,
                    "capabilities": deps.capabilities,
                },
            ),
        )
        attachments.extend(agg.attachments)

    # compact_result.projected_messages 是本次模型请求真正使用的消息历史；
    # 不一定等于 self.messages，因为可能已经被压缩过。
    context_messages = list(compact_result.projected_messages)
    context_messages.extend(attachment_to_user_message(att) for att in attachments)

    # system prompt 每次都重新生成，因为日期、工具列表、Plan Mode 状态、
    # 项目说明文件等都有可能变化。
    system_prompt = build_system_prompt(
        cwd=deps.cwd,
        tool_names=deps.tool_names,
        instruction_paths=deps.instruction_paths,
        plan_active=bool(getattr(deps.plan_mode_state, "active", False)),
        plan_file=getattr(deps.plan_mode_state, "plan_file", None),
    ).render()

    # 最后一步才把内部消息转 API 格式，这样前面的 hook/compact 都能使用内部结构。
    api_messages = normalize_messages_for_api(system_prompt, context_messages)
    return ContextBuildResult(system_prompt, context_messages, api_messages, compact_result, attachments)
