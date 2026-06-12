"""BigCode 内部的消息和内容块模型。

学习思路：内部消息比 API 消息更丰富，能表达用户文本、模型工具调用、工具结果和压缩摘要。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from bigcode.utils.ids import new_id


class TextBlock(BaseModel):
    """一段普通文本内容块。

    用户消息和助手消息都可以包含 TextBlock。
    """
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    """助手请求调用工具的内容块。

    id 会和后续 ToolResultBlock.tool_use_id 对应。
    """
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    """工具执行结果内容块。

    它通常出现在 meta UserMessage 中，用来把工具输出回传给模型。
    """
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: Any
    is_error: bool = False


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass
class MessageBase:
    """所有内部消息共有的基础字段。

    uuid/timestamp 用于追踪，is_meta/origin 用于区分真实用户输入和系统生成消息。
    """
    type: str
    uuid: str = field(default_factory=lambda: new_id("msg"))
    timestamp: float = field(default_factory=time.time)
    is_meta: bool = False
    origin: str = "user"


@dataclass
class UserMessage(MessageBase):
    """内部用户消息。

    既可以表示真实用户输入，也可以表示工具结果、hook 提醒等 meta 消息。
    """
    content: list[ContentBlock] = field(default_factory=list)

    def __init__(self, content: str | list[ContentBlock], *, is_meta: bool = False, origin: str = "user") -> None:
        """创建用户消息；字符串会自动包装成一个 TextBlock。"""
        super().__init__(type="user", is_meta=is_meta, origin=origin)
        self.content = [TextBlock(text=content)] if isinstance(content, str) else content


@dataclass
class AssistantMessage(MessageBase):
    """内部助手消息。

    保存模型返回的文本、工具调用、stop_reason 和 token usage。
    """
    content: list[ContentBlock] = field(default_factory=list)
    model: str | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        content: list[ContentBlock],
        *,
        model: str | None = None,
        stop_reason: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """创建助手消息，并保存模型名、停止原因和 token usage。"""
        super().__init__(type="assistant", origin="model")
        self.content = content
        self.model = model
        self.stop_reason = stop_reason
        self.usage = usage or {}


@dataclass
class SystemMessage(MessageBase):
    """内部系统消息。

    当前 transcript 主要恢复用户和助手消息，系统消息用于未来扩展或本地状态表达。
    """
    content: str = ""
    subtype: str = "info"
    level: str = "info"

    def __init__(self, content: str, *, subtype: str = "info", level: str = "info") -> None:
        """创建系统消息，用 subtype/level 描述消息用途和严重程度。"""
        super().__init__(type="system", origin="system")
        self.content = content
        self.subtype = subtype
        self.level = level


@dataclass
class ContextSummaryMessage(MessageBase):
    """上下文压缩摘要消息。

    当历史太长时，用它代替被省略的早期消息。
    """
    summary: str = ""

    def __init__(self, summary: str) -> None:
        """创建一条压缩摘要消息；它默认是 meta 消息，来源标记为 compact。"""
        super().__init__(type="context_summary", is_meta=True, origin="compact")
        self.summary = summary


class ApiMessage(BaseModel):
    """发给 Claude Messages API 的消息模型。

    它只保留 role 和 content，结构比 BigCode 内部消息更简单。
    """
    role: Literal["user", "assistant"]
    content: list[dict[str, Any]]


def text_from_blocks(blocks: list[ContentBlock]) -> str:
    """从内容块列表里提取所有 TextBlock 文本，并用换行拼接。"""
    return "\n".join(block.text for block in blocks if isinstance(block, TextBlock))
