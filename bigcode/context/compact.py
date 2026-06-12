"""对过长会话做简单压缩，避免模型上下文无限增长。

学习思路：目前实现很朴素，保留开头和结尾，中间用一条摘要消息代替，并压缩旧工具结果。
"""
from __future__ import annotations

from dataclasses import dataclass

from .messages import ContextSummaryMessage, MessageBase, ToolResultBlock, UserMessage


@dataclass
class ContextCompactState:
    """运行时状态对象。

    字段主要记录当前流程走到哪里，通常会被会话或工具持续更新。
    """
    step_index: int = 0
    turn_start: bool = True


@dataclass
class ContextCompactResult:
    """结构化结果数据。

    这种 dataclass 主要用于在模块之间传递信息，比直接使用 dict 更容易看出字段含义。
    """
    projected_messages: list[MessageBase]
    micro_compacted: bool = False
    snipped: bool = False
    collapsed_spans: int = 0
    auto_compacted: bool = False
    blocked: bool = False


async def apply_context_compact(messages: list[MessageBase], *, max_messages: int = 120) -> ContextCompactResult:
    """对消息列表做粗粒度压缩。

    消息数未超过 max_messages 时原样返回；超过后保留头尾并插入摘要。
    """
    if len(messages) <= max_messages:
        return ContextCompactResult(projected_messages=list(messages))
    keep_head = messages[:10]
    keep_tail = messages[-80:]
    summary = ContextSummaryMessage(f"Earlier conversation compacted. {len(messages) - len(keep_head) - len(keep_tail)} messages omitted.")
    projected = keep_head + [summary] + keep_tail
    return ContextCompactResult(projected_messages=micro_compact_tool_results(projected), micro_compacted=True, snipped=True)


def micro_compact_tool_results(messages: list[MessageBase], keep_recent: int = 3) -> list[MessageBase]:
    """压缩较旧的工具结果内容。

    最近 keep_recent 个工具结果保留原文，更早的只留下占位文本。
    """
    tool_result_indices = [
        idx
        for idx, msg in enumerate(messages)
        if isinstance(msg, UserMessage) and any(isinstance(block, ToolResultBlock) for block in msg.content)
    ]
    preserve = set(tool_result_indices[-keep_recent:])
    out: list[MessageBase] = []
    for idx, msg in enumerate(messages):
        if idx in tool_result_indices and idx not in preserve and isinstance(msg, UserMessage):
            compacted = []
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    compacted.append(ToolResultBlock(tool_use_id=block.tool_use_id, content="[Older tool result compacted]", is_error=block.is_error))
                else:
                    compacted.append(block)
            out.append(UserMessage(compacted, is_meta=msg.is_meta, origin=msg.origin))
        else:
            out.append(msg)
    return out

