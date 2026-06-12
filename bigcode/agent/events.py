"""定义 Agent 运行时对外发送的事件类型。

学习思路：这些 dataclass 只是数据容器，事件最终会被 gateway 序列化成 JSONL，方便前端或脚本监听。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Union


def _now() -> float:
    """返回当前 Unix 时间戳，作为事件默认 timestamp。"""
    return time.time()


@dataclass(frozen=True)
class StreamEvent:
    """流式文本事件。

    如果以后模型支持边生成边输出，可以用它把文本片段推给外部 UI。
    """
    session_id: str
    text: str
    event_type: Literal["stream"] = "stream"
    timestamp: float = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StatusEvent:
    """状态事件。

    用于报告 session_started、turn_started、model_request_started 等阶段性状态。
    """
    session_id: str
    status: str
    event_type: Literal["status"] = "status"
    timestamp: float = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ErrorEvent:
    """错误事件。

    可以关联到具体 tool_use_id/tool_name，也可以表示会话级错误。
    """
    session_id: str
    message: str
    event_type: Literal["error"] = "error"
    timestamp: float = field(default_factory=_now)
    tool_use_id: str | None = None
    tool_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolStarted:
    """工具开始事件。

    ToolRunner 在真正执行工具前发送它，方便外部界面显示当前正在运行什么。
    """
    session_id: str
    tool_use_id: str
    tool_name: str
    event_type: Literal["tool_started"] = "tool_started"
    timestamp: float = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCompleted:
    """工具完成事件。

    记录工具是否出错和耗时，metadata 可携带结果截断等补充信息。
    """
    session_id: str
    tool_use_id: str
    tool_name: str
    is_error: bool
    duration_ms: int
    event_type: Literal["tool_completed"] = "tool_completed"
    timestamp: float = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnCompleted:
    """单轮对话完成事件。

    包含最终 assistant 文本、停止原因和本轮工具结果数量。
    """
    session_id: str
    assistant_text: str
    stop_reason: str | None
    tool_result_count: int
    event_type: Literal["turn_completed"] = "turn_completed"
    timestamp: float = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=dict)


AgentEvent = Union[StreamEvent, StatusEvent, ErrorEvent, ToolStarted, ToolCompleted, TurnCompleted]
EventSink = Callable[[AgentEvent], None]
