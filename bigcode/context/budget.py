"""Per-message aggregate tool result budget with freezing.

Applies two thresholds to tool results before they reach the model:
1. Single-result: > 50K chars -> spill to disk, show <persisted-output> preview
2. Aggregate: all results in one message > 200K chars -> spill largest until under budget

Decisions are frozen in ToolBudgetState to guarantee prompt cache byte-stability.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from bigcode.tools.artifacts import ArtifactStore
from bigcode.tools.artifacts.store import serialized_chars
from bigcode.utils.jsonio import to_jsonable

from .messages import (
    AssistantMessage,
    MessageBase,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

SINGLE_RESULT_CHAR_LIMIT = 50_000
AGGREGATE_CHAR_LIMIT = 200_000
PREVIEW_CHARS = 2_000
PERSISTED_TAG = "<persisted-output>"


@dataclass
class ToolBudgetState:
    """Frozen decisions for tool result spill/replacement.

    seen_ids: every tool_use_id ever evaluated (decision frozen)
    replacements: subset of seen_ids that were spilled -> replacement text
    """

    seen_ids: set[str] = field(default_factory=set)
    replacements: dict[str, str] = field(default_factory=dict)


def make_persisted_preview(content: Any, artifact_path: str, original_chars: int) -> str:
    """Build the <persisted-output> preview string for a spilled tool result."""
    size_kb = original_chars // 1000
    preview = _extract_preview_text(content)
    return (
        f"{PERSISTED_TAG}\n"
        f"输出太大（{size_kb}KB），完整内容已保存到：\n"
        f"{artifact_path}\n\n"
        f"预览（前{PREVIEW_CHARS // 1000}KB）：\n"
        f"{preview}\n"
        f"</persisted-output>"
    )


def _extract_preview_text(content: Any) -> str:
    """Extract the first PREVIEW_CHARS characters from tool result content."""
    if isinstance(content, str):
        return content[:PREVIEW_CHARS]
    if isinstance(content, dict):
        for key in ("stdout", "content", "text", "result", "output"):
            val = content.get(key)
            if isinstance(val, str) and val:
                return val[:PREVIEW_CHARS]
        return str(to_jsonable(content))[:PREVIEW_CHARS]
    return str(to_jsonable(content))[:PREVIEW_CHARS]


def _content_chars(content: Any) -> int:
    """Measure serialized character length of tool result content."""
    if isinstance(content, str):
        return len(content)
    return serialized_chars(content)


def _build_tool_name_map(messages: list[MessageBase]) -> dict[str, str]:
    """Scan AssistantMessages for ToolUseBlocks to build tool_use_id -> tool_name map."""
    name_map: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    name_map[block.id] = block.name
    return name_map


def apply_tool_result_budget(
    messages: list[MessageBase],
    state: ToolBudgetState,
    artifact_store: ArtifactStore,
) -> list[MessageBase]:
    """Return a projected message list with large tool results replaced by previews.

    Does NOT mutate the original messages. Decisions are frozen in *state* so
    subsequent calls with the same state are zero-cost lookups for frozen results.
    """
    tool_name_map = _build_tool_name_map(messages)

    # === Pass 0: Categorize ===
    # (msg_idx, block, content_chars) for results that need fresh evaluation
    fresh_entries: list[tuple[int, ToolResultBlock, int]] = []

    for msg_idx, msg in enumerate(messages):
        if not isinstance(msg, UserMessage):
            continue
        for block in msg.content:
            if not isinstance(block, ToolResultBlock):
                continue
            tid = block.tool_use_id

            # Already spilled -> skip (cached replacement applied in output build)
            if tid in state.replacements:
                continue
            # Already seen and kept -> skip (frozen)
            if tid in state.seen_ids:
                continue
            # Already a persisted preview from an external spill path
            if isinstance(block.content, str) and block.content.startswith(PERSISTED_TAG):
                state.seen_ids.add(tid)
                state.replacements[tid] = block.content
                continue

            chars = _content_chars(block.content)
            fresh_entries.append((msg_idx, block, chars))

    # === Pass 1: Single-result threshold ===
    for msg_idx, block, chars in fresh_entries:
        if chars <= SINGLE_RESULT_CHAR_LIMIT:
            continue
        _spill_one(block, chars, tool_name_map, artifact_store, state)

    # === Pass 2: Aggregate threshold (per-message) ===
    msg_entries: dict[int, list[tuple[ToolResultBlock, int]]] = defaultdict(list)
    for msg_idx, block, chars in fresh_entries:
        if block.tool_use_id not in state.replacements:
            msg_entries[msg_idx].append((block, chars))

    for msg_idx, entries in msg_entries.items():
        msg = messages[msg_idx]
        total = _message_tool_result_total(msg, state)
        if total <= AGGREGATE_CHAR_LIMIT:
            continue

        ranked = sorted(entries, key=lambda e: e[1], reverse=True)
        for block, chars in ranked:
            if total <= AGGREGATE_CHAR_LIMIT:
                break
            _spill_one(block, chars, tool_name_map, artifact_store, state)
            total -= chars - len(state.replacements[block.tool_use_id])

    # === Finalize: Freeze remaining fresh as "seen but kept" ===
    for _msg_idx, block, _chars in fresh_entries:
        if block.tool_use_id not in state.seen_ids:
            state.seen_ids.add(block.tool_use_id)

    # === Build output with all known replacements applied ===
    result: list[MessageBase] = []
    for msg in messages:
        if not isinstance(msg, UserMessage):
            result.append(msg)
            continue

        has_changed = False
        new_blocks: list[Any] = []
        for block in msg.content:
            if isinstance(block, ToolResultBlock) and block.tool_use_id in state.replacements:
                has_changed = True
                new_blocks.append(
                    ToolResultBlock(
                        tool_use_id=block.tool_use_id,
                        content=state.replacements[block.tool_use_id],
                        is_error=block.is_error,
                    )
                )
            else:
                new_blocks.append(block)

        if has_changed:
            result.append(
                UserMessage(
                    new_blocks,
                    is_meta=msg.is_meta,
                    origin=msg.origin,
                    uuid=msg.uuid,
                    timestamp=msg.timestamp,
                )
            )
        else:
            result.append(msg)

    return result


def _message_tool_result_total(msg: MessageBase, state: ToolBudgetState) -> int:
    """Sum serialized chars of all ToolResultBlocks in a message, counting replacements at preview length."""
    if not isinstance(msg, UserMessage):
        return 0
    total = 0
    for block in msg.content:
        if not isinstance(block, ToolResultBlock):
            continue
        if block.tool_use_id in state.replacements:
            total += len(state.replacements[block.tool_use_id])
        else:
            total += _content_chars(block.content)
    return total


def _spill_one(
    block: ToolResultBlock,
    chars: int,
    tool_name_map: dict[str, str],
    artifact_store: ArtifactStore,
    state: ToolBudgetState,
) -> None:
    """Write a tool result to disk and record the replacement preview in state."""
    artifact_path: str | None = None
    if isinstance(block.content, dict):
        raw = block.content.get("artifact_path")
        if isinstance(raw, str):
            artifact_path = raw

    if artifact_path is None:
        tool_name = tool_name_map.get(block.tool_use_id, "unknown")
        record = artifact_store.write_tool_output(
            tool_use_id=block.tool_use_id,
            tool_name=tool_name,
            output=block.content if not block.is_error else None,
            is_error=block.is_error,
            error_message=str(block.content) if block.is_error else "",
        )
        artifact_path = record.artifact_path
        original_chars = record.original_chars
    else:
        raw_original = block.content.get("original_chars") if isinstance(block.content, dict) else None
        original_chars = raw_original if isinstance(raw_original, int) else chars

    preview = make_persisted_preview(block.content, artifact_path, original_chars)
    state.replacements[block.tool_use_id] = preview
    state.seen_ids.add(block.tool_use_id)
