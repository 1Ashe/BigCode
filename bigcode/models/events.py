"""模型流式事件定义。

这些事件是 AgentSession 和具体 Provider SDK 之间的稳定边界。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias


@dataclass(frozen=True, slots=True)
class TextDelta:
    """模型新增的一段可见文本。"""

    text: str


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    """模型新增的一段 thinking 文本。"""

    thinking: str


@dataclass(frozen=True, slots=True)
class ThinkingComplete:
    """一个 thinking 块结束。"""

    thinking: str
    signature: str


@dataclass(frozen=True, slots=True)
class ToolCallStart:
    """工具调用块开始。"""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """工具调用参数 JSON 增量。"""

    id: str
    partial_json: str


@dataclass(frozen=True, slots=True)
class ToolCallComplete:
    """工具调用块结束，参数已经解析为 dict。"""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StreamEnd:
    """一次模型流结束。"""

    stop_reason: str | None
    input_tokens: int
    output_tokens: int


StreamEvent: TypeAlias = (
    TextDelta
    | ThinkingDelta
    | ThinkingComplete
    | ToolCallStart
    | ToolCallDelta
    | ToolCallComplete
    | StreamEnd
)
