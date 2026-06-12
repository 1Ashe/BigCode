"""把会话消息追加保存为 JSONL，并能从磁盘恢复。

学习思路：每行是一个 JSON 对象，message_class 用来恢复成 UserMessage 或 AssistantMessage。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from bigcode.utils.jsonio import append_jsonl, read_jsonl, to_jsonable

from .messages import AssistantMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage


class Transcript:
    """会话 transcript 文件封装。

    负责把消息按 JSONL 追加保存，也负责启动 resume 时把历史消息加载回来。
    """
    def __init__(self, path: Path) -> None:
        """保存 transcript 文件路径，真正创建目录发生在 append_jsonl 写入时。"""
        self.path = path

    def append(self, message: Any) -> None:
        """把一条消息序列化后追加到 JSONL transcript。"""
        append_jsonl(self.path, serialize_message(message))

    def load(self) -> list[Any]:
        """读取 transcript 文件，并尽量恢复其中可识别的消息对象。"""
        messages = []
        for row in read_jsonl(self.path):
            msg = deserialize_message(row)
            if msg is not None:
                messages.append(msg)
        return messages


def serialize_message(message: Any) -> dict[str, Any]:
    """把内部对象转换成可写入 JSON 或传给外部系统的普通结构。"""
    data = to_jsonable(message)
    data["message_class"] = type(message).__name__
    return data


def deserialize_message(row: dict[str, Any]) -> Any | None:
    """把普通 JSON 数据恢复成项目内部对象。"""
    cls = row.get("message_class")
    if cls == "UserMessage":
        blocks = [_block_from_dict(b) for b in row.get("content") or []]
        return UserMessage([b for b in blocks if b is not None], is_meta=bool(row.get("is_meta")), origin=row.get("origin", "user"))
    if cls == "AssistantMessage":
        blocks = [_block_from_dict(b) for b in row.get("content") or []]
        return AssistantMessage([b for b in blocks if b is not None], model=row.get("model"), stop_reason=row.get("stop_reason"), usage=row.get("usage") or {})
    return None


def _block_from_dict(data: dict[str, Any]) -> Any | None:
    """把 transcript 里的内容块 dict 恢复为 Text/ToolUse/ToolResult block。"""
    if not isinstance(data, dict):
        return None
    typ = data.get("type")
    if typ == "text":
        return TextBlock(text=data.get("text", ""))
    if typ == "tool_use":
        return ToolUseBlock(id=data.get("id", ""), name=data.get("name", ""), input=data.get("input") or {})
    if typ == "tool_result":
        return ToolResultBlock(tool_use_id=data.get("tool_use_id", ""), content=data.get("content"), is_error=bool(data.get("is_error")))
    return None
