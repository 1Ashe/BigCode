"""Hook 系统使用的数据结构。

学习思路：HookInput 是事件输入，HookOutput 是单个 handler 的返回，HookAggregate 是多个 handler 合并后的结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


HookEvent = Literal[
    "SessionStart",
    "UserPromptSubmit",
    "ContextBuild",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
    "PlanModeEnter",
    "PlanModeExit",
    "TaskCreated",
    "TaskUpdated",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "CapabilityChanged",
]
HookDecision = Literal["approve", "ask", "block", "passthrough"]
HookSource = Literal["built-in", "user"]


@dataclass
class HookInput:
    """工具输入模型。

    Pydantic 会根据这些字段校验模型传进来的参数，字段类型就是最重要的阅读线索。
    """
    hook_event_name: HookEvent
    session_id: str
    cwd: str
    permission_mode: str
    transcript_path: str | None = None
    agent_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookOutput:
    """单个 hook handler 的返回值。

    可以批准/阻止/询问、修改工具输入、追加上下文或要求继续当前 turn。
    """
    decision: HookDecision = "passthrough"
    reason: str = ""
    updated_input: dict[str, Any] | None = None
    additional_context: str | None = None
    attachments: list[Any] = field(default_factory=list)
    continue_turn: bool | None = None
    stop_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResult:
    """结构化结果数据。

    这种 dataclass 主要用于在模块之间传递信息，比直接使用 dict 更容易看出字段含义。
    """
    event: HookEvent
    source: HookSource
    name: str
    output: HookOutput
    duration_ms: int
    error: str | None = None


@dataclass
class HookAggregate:
    """同一事件下多个 hook 的合并结果。

    HookBus.emit 返回它，调用方只需要看最终 decision、attachments、updated_input 等字段。
    """
    event: HookEvent
    results: list[HookResult]
    decision: HookDecision = "passthrough"
    reason: str = ""
    updated_input: dict[str, Any] | None = None
    attachments: list[Any] = field(default_factory=list)
    continue_turn: bool = False
    stop_reason: str | None = None
