"""把内部 MessageBase 转换成 Claude Messages API 能接收的格式。

学习思路：重点看 pending_tool_use_ids，它保证每个 tool_use 后面都有对应 tool_result，这是工具调用协议要求。
"""
from __future__ import annotations

from typing import Any

from bigcode.tools.base import ToolRunResult
from bigcode.utils.jsonio import to_jsonable

from .attachments import Attachment, wrap_system_reminder
from .messages import (
    ApiMessage,
    AssistantMessage,
    ContextSummaryMessage,
    MessageBase,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


def attachment_to_user_message(att: Attachment) -> UserMessage:
    """把 Attachment 包装成 meta UserMessage，最终让模型作为系统提醒看到。"""
    return UserMessage(wrap_system_reminder(att.text), is_meta=True, origin=att.source)


def normalize_messages_for_api(system_prompt: str, messages: list[MessageBase]) -> list[ApiMessage]:
    """把内部消息流投影成 Claude Messages API 格式。

    重点是维护 pending_tool_use_ids，确保 assistant 的 tool_use 后紧跟 user 的 tool_result。
    """
    _ = system_prompt
    api: list[ApiMessage] = []

    # Claude 工具协议要求 assistant 发出 tool_use 后，后续 user 消息必须带回
    # 对应的 tool_result。这个集合专门追踪“还没收到结果”的 tool_use id。
    pending_tool_use_ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, UserMessage):
            blocks: list[dict[str, Any]] = []
            text_parts: list[str] = []
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if block.text:
                        text_parts.append(block.text)
                elif isinstance(block, ToolResultBlock):
                    if block.tool_use_id in pending_tool_use_ids:
                        # 正常路径：这个工具结果正好回应之前 assistant 的 tool_use。
                        blocks.append(_tool_result_to_api_block(block))
                        pending_tool_use_ids.discard(block.tool_use_id)
                    else:
                        # transcript 可能来自旧版本或手工修改。孤儿 tool_result 不能直接
                        # 发给 API，于是降级成普通文本，保证请求仍然合法。
                        text_parts.append(
                            "Orphaned tool result ignored by API projection "
                            f"for tool_use_id={block.tool_use_id}:\n{_stringify_tool_content(block.content, is_error=block.is_error)}"
                        )
                elif isinstance(block, ToolUseBlock):
                    text_parts.append(f"Unexpected tool_use block in user message: {block.name}")
            if text_parts and pending_tool_use_ids:
                # 如果工具结果缺失但接下来已经有普通用户文本，必须先补错误 tool_result，
                # 否则 API 会认为上一个 assistant tool_use 没有闭合。
                blocks.extend(_missing_tool_result_blocks(pending_tool_use_ids))
                pending_tool_use_ids.clear()
            if text_parts:
                blocks.append({"type": "text", "text": "\n".join(text_parts)})
            if blocks:
                api.append(ApiMessage(role="user", content=blocks))
        elif isinstance(msg, AssistantMessage):
            if pending_tool_use_ids:
                # 新 assistant 消息到来前，旧的 tool_use 也必须闭合。
                api.append(ApiMessage(role="user", content=_missing_tool_result_blocks(pending_tool_use_ids)))
                pending_tool_use_ids.clear()
            blocks = []
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if block.text:
                        blocks.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolUseBlock):
                    blocks.append({"type": "tool_use", "id": block.id, "name": block.name, "input": to_jsonable(block.input)})
                    pending_tool_use_ids.add(block.id)
            if blocks:
                api.append(ApiMessage(role="assistant", content=blocks))
        elif isinstance(msg, ContextSummaryMessage):
            if pending_tool_use_ids:
                api.append(ApiMessage(role="user", content=_missing_tool_result_blocks(pending_tool_use_ids)))
                pending_tool_use_ids.clear()
            # 摘要不是用户原话，包装成 system-reminder 能降低模型把它当用户需求的概率。
            api.append(ApiMessage(role="user", content=[{"type": "text", "text": wrap_system_reminder(msg.summary)}]))
    if pending_tool_use_ids:
        api.append(ApiMessage(role="user", content=_missing_tool_result_blocks(pending_tool_use_ids)))
    return _merge_adjacent_users(api)


def tool_run_result_to_message(result: ToolRunResult[Any]) -> UserMessage:
    """把 ToolRunResult 变成一条 meta UserMessage，其中包含 ToolResultBlock。"""
    content: Any = result.error_message if result.is_error else (result.output.data if result.output else "")
    artifact_metadata = _artifact_metadata(result)
    if artifact_metadata:
        if isinstance(content, dict):
            content = {**content, **artifact_metadata}
        else:
            content = {"result": content, **artifact_metadata}
    return UserMessage([ToolResultBlock(tool_use_id=result.tool_use_id, content=to_jsonable(content), is_error=result.is_error)], is_meta=True, origin="tool")


def _artifact_metadata(result: ToolRunResult[Any]) -> dict[str, Any]:
    """从工具结果 metadata 中提取 artifact 相关字段。"""
    metadata = dict(result.metadata)
    if result.output:
        metadata.update(result.output.metadata)
    out: dict[str, Any] = {}
    for key in ("artifact_id", "artifact_path", "original_chars"):
        if key in metadata:
            out[key] = metadata[key]
    return out


def _stringify_tool_content(content: Any, *, is_error: bool) -> str:
    """把工具结果内容转成 API 需要的字符串；错误结果会加 ERROR 前缀。"""
    prefix = "ERROR: " if is_error else ""
    if isinstance(content, str):
        return prefix + content
    return prefix + str(to_jsonable(content))


def _tool_result_to_api_block(result: ToolResultBlock) -> dict[str, Any]:
    """把内部 ToolResultBlock 转成 Claude API 的 tool_result 块。"""
    return {
        "type": "tool_result",
        "tool_use_id": result.tool_use_id,
        "content": _stringify_tool_content(result.content, is_error=result.is_error),
        "is_error": result.is_error,
    }


def _missing_tool_result_blocks(tool_use_ids: set[str]) -> list[dict[str, Any]]:
    """为缺失的 tool_use 生成错误 tool_result，防止 API 因协议不完整而报错。"""
    return [
        {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": "ERROR: Tool result missing from transcript.",
            "is_error": True,
        }
        for tool_use_id in sorted(tool_use_ids)
    ]


def _merge_adjacent_users(messages: list[ApiMessage]) -> list[ApiMessage]:
    """合并相邻 user 消息，减少 API message 数量并保持协议合法。"""
    merged: list[ApiMessage] = []
    for msg in messages:
        if merged and msg.role == "user" and merged[-1].role == "user":
            merged[-1].content.extend(msg.content)
        else:
            merged.append(msg)
    return merged
