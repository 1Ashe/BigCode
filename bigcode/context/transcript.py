"""把会话消息追加保存为 JSONL，并能从磁盘恢复。

学习思路：每行是一个 JSON 对象，message_class 用来恢复成 UserMessage 或 AssistantMessage。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from bigcode.utils.jsonio import append_jsonl, read_jsonl, to_jsonable

from .messages import (
    AssistantMessage,
    CompactRecordMessage,
    ContextSummaryMessage,
    SystemMessage,
    SystemPromptSnapshotMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


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
        return truncate_to_complete_tool_chain(messages)


def serialize_message(message: Any) -> dict[str, Any]:
    """把内部对象转换成可写入 JSON 或传给外部系统的普通结构。"""
    data = to_jsonable(message)
    data["message_class"] = type(message).__name__
    return data


def deserialize_message(row: dict[str, Any]) -> Any | None:
    """把普通 JSON 数据恢复成项目内部对象。"""
    cls = row.get("message_class")
    identity = {
        "uuid": str(row.get("uuid") or "") or None,
        "timestamp": _float_or_none(row.get("timestamp")),
    }
    if cls == "UserMessage":
        blocks = [_block_from_dict(b) for b in row.get("content") or []]
        return UserMessage(
            [b for b in blocks if b is not None],
            is_meta=bool(row.get("is_meta")),
            origin=row.get("origin", "user"),
            **identity,
        )
    if cls == "AssistantMessage":
        blocks = [_block_from_dict(b) for b in row.get("content") or []]
        return AssistantMessage(
            [b for b in blocks if b is not None],
            model=row.get("model"),
            stop_reason=row.get("stop_reason"),
            usage=row.get("usage") or {},
            **identity,
        )
    if cls == "SystemMessage":
        return SystemMessage(
            str(row.get("content") or ""),
            subtype=str(row.get("subtype") or "info"),
            level=str(row.get("level") or "info"),
            **identity,
        )
    if cls == "ContextSummaryMessage":
        return ContextSummaryMessage(str(row.get("summary") or ""), **identity)
    if cls == "SystemPromptSnapshotMessage":
        return SystemPromptSnapshotMessage(str(row.get("prompt") or ""), **identity)
    if cls == "CompactRecordMessage":
        compact_type = row.get("compact_type")
        if compact_type not in {"snip", "time_micro", "collapse", "auto"}:
            return None
        return CompactRecordMessage(
            compact_type,
            covered_message_ids=_string_list(row.get("covered_message_ids")),
            superseded_record_ids=_string_list(row.get("superseded_record_ids")),
            cleared_tool_use_ids=_string_list(row.get("cleared_tool_use_ids")),
            summary=str(row["summary"]) if row.get("summary") is not None else None,
            tokens_before=_int_or_zero(row.get("tokens_before")),
            tokens_after=_int_or_zero(row.get("tokens_after")),
            created_step=_int_or_zero(row.get("created_step")),
            created_turn=_int_or_zero(row.get("created_turn")),
            **identity,
        )
    return None


def truncate_to_complete_tool_chain(messages: list[Any]) -> list[Any]:
    """Drop the trailing suffix after the last fully closed tool-call point."""
    pending: set[str] = set()
    last_complete = len(messages)
    for idx, msg in enumerate(messages):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock) and block.id:
                    pending.add(block.id)
        elif isinstance(msg, UserMessage):
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    pending.discard(block.tool_use_id)
        if not pending:
            last_complete = idx + 1
    return messages[:last_complete]


def _block_from_dict(data: dict[str, Any]) -> Any | None:
    """把 transcript 里的内容块 dict 恢复为 Text/ToolUse/ToolResult block。"""
    if not isinstance(data, dict):
        return None
    typ = data.get("type")
    if typ == "text":
        return TextBlock(text=data.get("text", ""))
    if typ == "thinking":
        return ThinkingBlock(thinking=data.get("thinking", ""), signature=data.get("signature", ""))
    if typ == "tool_use":
        return ToolUseBlock(id=data.get("id", ""), name=data.get("name", ""), input=data.get("input") or {})
    if typ == "tool_result":
        return ToolResultBlock(tool_use_id=data.get("tool_use_id", ""), content=data.get("content"), is_error=bool(data.get("is_error")))
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
