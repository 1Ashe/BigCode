"""基于持久化压缩记录生成稳定的模型上下文投影。"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

from bigcode.config.models import CompactConfig
from bigcode.utils.jsonio import to_jsonable

from .messages import (
    AssistantMessage,
    CompactRecordMessage,
    ContextSummaryMessage,
    MessageBase,
    SystemMessage,
    SystemPromptSnapshotMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


TIME_BASED_CLEARED_MESSAGE = "[Old tool result content cleared]"
COMPACTABLE_TOOLS = {
    "read",
    "bash",
    "grep",
    "glob",
    "websearch",
    "webfetch",
    "edit",
    "write",
}
EDIT_TOOLS = {"edit", "write", "fileedit", "filewrite", "apply_patch"}
ERROR_MARKERS = ("error", "failed", "failure", "exception", "traceback", "permission denied")
SummaryCallback = Callable[[str, str], Awaitable[str]]


@dataclass
class ContextCompactState:
    """只保存不能从 UI Context 重建的运行时压缩状态。"""

    turn_index: int = 0
    step_index: int = 0
    snip_applied_turn: int | None = None
    auto_compact_failures: int = 0

    @property
    def turn_start(self) -> bool:
        return self.step_index == 0


@dataclass
class CompactDeps:
    """Compact 算法需要的稳定配置和外部能力。"""

    config: CompactConfig = field(default_factory=CompactConfig)
    state: ContextCompactState = field(default_factory=ContextCompactState)
    context_window: int = 128000
    system_prompt: str = ""
    tool_schemas: list[dict] = field(default_factory=list)
    extra_context_messages: list[MessageBase] = field(default_factory=list)
    is_main_thread: bool = True
    summarize: SummaryCallback | None = None


@dataclass
class ContextCompactResult:
    """一次压缩调度的投影和新增持久化事件。"""

    projected_messages: list[MessageBase]
    records_to_append: list[CompactRecordMessage] = field(default_factory=list)
    tokens_before: int = 0
    tokens_after: int = 0
    utilization_before: float = 0.0
    utilization_after: float = 0.0
    micro_compacted: bool = False
    snipped: bool = False
    collapsed_spans: int = 0
    auto_compacted: bool = False
    blocked: bool = False


@dataclass
class MessageGroup:
    start: int
    end: int
    messages: list[MessageBase]
    tokens: int
    protected: bool = False
    reasons: set[str] = field(default_factory=set)
    tool_names: set[str] = field(default_factory=set)
    has_error: bool = False

    @property
    def message_count(self) -> int:
        return len(self.messages)


async def apply_context_compact(
    messages: list[MessageBase],
    deps: CompactDeps | None = None,
    *,
    force_auto: bool = False,
) -> ContextCompactResult:
    """重放已有记录，再按 Snip/Micro/Collapse/Auto 顺序生成新记录。"""
    deps = deps or CompactDeps()
    working = list(messages)
    records: list[CompactRecordMessage] = []
    projected = replay_compact_records(working)
    tokens_before = estimate_context_tokens(projected, deps)
    utilization_before = _utilization(tokens_before, deps.context_window)
    current_tokens = tokens_before
    current_utilization = utilization_before
    snipped = False
    micro_compacted = False
    collapsed_spans = 0
    auto_compacted = False

    if not force_auto and (
        deps.config.snip_enabled
        and deps.state.snip_applied_turn != deps.state.turn_index
        and current_utilization >= deps.config.snip_threshold
    ):
        record = _build_snip_record(working, projected, deps, current_tokens)
        if record:
            working.append(record)
            records.append(record)
            deps.state.snip_applied_turn = deps.state.turn_index
            projected = replay_compact_records(working)
            current_tokens = estimate_context_tokens(projected, deps)
            record.tokens_after = current_tokens
            current_utilization = _utilization(current_tokens, deps.context_window)
            snipped = True

    if not force_auto:
        micro_record = _build_time_microcompact_record(working, projected, deps, current_tokens)
        if micro_record:
            working.append(micro_record)
            records.append(micro_record)
            projected = replay_compact_records(working)
            current_tokens = estimate_context_tokens(projected, deps)
            micro_record.tokens_after = current_tokens
            current_utilization = _utilization(current_tokens, deps.context_window)
            micro_compacted = True
        else:
            await apply_cached_microcompact(projected, deps)

    if not force_auto and deps.config.context_collapse_enabled:
        while (
            collapsed_spans < deps.config.collapse_max_spans_per_pass
            and current_utilization >= deps.config.collapse_threshold
        ):
            record = await _build_collapse_record(working, projected, deps, current_tokens)
            if not record:
                break
            working.append(record)
            records.append(record)
            projected = replay_compact_records(working)
            new_tokens = estimate_context_tokens(projected, deps)
            if new_tokens >= current_tokens:
                working.pop()
                records.pop()
                projected = replay_compact_records(working)
                break
            record.tokens_after = new_tokens
            current_tokens = new_tokens
            current_utilization = _utilization(current_tokens, deps.context_window)
            collapsed_spans += 1
    elif force_auto or (
        deps.config.auto_compact_enabled
        and deps.state.turn_start
        and current_utilization >= deps.config.auto_compact_threshold
        and deps.state.auto_compact_failures < deps.config.auto_max_failures
    ):
        if not force_auto:
            await apply_fast_compact(projected, deps)
        try:
            record = await _build_auto_record(working, projected, deps, current_tokens, force=force_auto)
        except RuntimeError:
            deps.state.auto_compact_failures += 1
            record = None
        if record:
            working.append(record)
            projected_after = replay_compact_records(working)
            new_tokens = estimate_context_tokens(projected_after, deps)
            if new_tokens < current_tokens:
                record.tokens_after = new_tokens
                records.append(record)
                projected = projected_after
                current_tokens = new_tokens
                current_utilization = _utilization(current_tokens, deps.context_window)
                deps.state.auto_compact_failures = 0
                auto_compacted = True
            else:
                working.pop()
                deps.state.auto_compact_failures += 1

    blocked = current_utilization >= deps.config.blocked_threshold
    return ContextCompactResult(
        projected_messages=projected,
        records_to_append=records,
        tokens_before=tokens_before,
        tokens_after=current_tokens,
        utilization_before=utilization_before,
        utilization_after=current_utilization,
        micro_compacted=micro_compacted,
        snipped=snipped,
        collapsed_spans=collapsed_spans,
        auto_compacted=auto_compacted,
        blocked=blocked,
    )


def replay_compact_records(messages: list[MessageBase]) -> list[MessageBase]:
    """从完整 UI 事件流确定性重放当前有效的 API Context 投影。"""
    records = [message for message in messages if isinstance(message, CompactRecordMessage)]
    superseded = {
        record_id
        for record in records
        for record_id in record.superseded_record_ids
    }
    active = [record for record in records if record.uuid not in superseded]
    hidden_ids = {
        message_id
        for record in active
        if record.compact_type in {"snip", "collapse", "auto"}
        for message_id in record.covered_message_ids
    }
    cleared_tool_ids = {
        tool_id
        for record in active
        if record.compact_type == "time_micro"
        for tool_id in record.cleared_tool_use_ids
    }
    originals = [
        message
        for message in messages
        if not isinstance(message, (CompactRecordMessage, SystemPromptSnapshotMessage))
    ]
    positions = {message.uuid: index for index, message in enumerate(originals)}
    insertions: dict[int, list[CompactRecordMessage]] = {}
    for record in active:
        if record.compact_type not in {"snip", "collapse", "auto"} or not record.summary:
            continue
        covered_positions = [positions[item] for item in record.covered_message_ids if item in positions]
        if not covered_positions:
            continue
        insertions.setdefault(min(covered_positions), []).append(record)

    projected: list[MessageBase] = []
    for index, message in enumerate(originals):
        for record in insertions.get(index, []):
            projected.append(
                ContextSummaryMessage(record.summary or "", uuid=record.uuid, timestamp=record.timestamp)
            )
        if message.uuid in hidden_ids:
            continue
        projected.append(_project_tool_result_clears(message, cleared_tool_ids))
    return projected


def estimate_context_tokens(messages: list[MessageBase], deps: CompactDeps) -> int:
    """估算完整请求输入，包括固定 prompt、工具 schema 和动态附件。"""
    total = estimate_messages_tokens(messages)
    total += estimate_text_tokens(deps.system_prompt)
    if deps.tool_schemas:
        total += estimate_text_tokens(_json_text(deps.tool_schemas))
    total += estimate_messages_tokens(deps.extra_context_messages)
    return total


def estimate_messages_tokens(messages: Iterable[MessageBase]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def estimate_message_tokens(message: MessageBase) -> int:
    total = 0
    if isinstance(message, (UserMessage, AssistantMessage)):
        for block in message.content:
            if isinstance(block, TextBlock):
                total += estimate_text_tokens(block.text)
            elif isinstance(block, ThinkingBlock):
                total += estimate_text_tokens(block.thinking)
            elif isinstance(block, ToolUseBlock):
                total += estimate_text_tokens(block.name + _json_text(block.input))
            elif isinstance(block, ToolResultBlock):
                total += _estimate_tool_result_tokens(block.content)
    elif isinstance(message, ContextSummaryMessage):
        total += estimate_text_tokens(message.summary)
    elif isinstance(message, SystemMessage):
        total += estimate_text_tokens(message.content)
    return max(1, math.ceil(total * 4 / 3)) if total else 0


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(max(len(text), len(text.encode("utf-8"))) / 4))


def build_message_groups(messages: list[MessageBase]) -> list[MessageGroup]:
    """按 tool_use/tool_result 完整性构造不重叠消息组。"""
    result_indexes: dict[str, list[int]] = {}
    orphan_indexes: set[int] = set()
    call_indexes: dict[str, int] = {}
    for index, message in enumerate(messages):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    call_indexes[block.id] = index
        elif isinstance(message, UserMessage):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    result_indexes.setdefault(block.tool_use_id, []).append(index)
    for index, message in enumerate(messages):
        if isinstance(message, UserMessage) and any(
            isinstance(block, ToolResultBlock)
            and (
                block.tool_use_id not in call_indexes
                or call_indexes[block.tool_use_id] >= index
            )
            for block in message.content
        ):
            orphan_indexes.add(index)

    intervals: list[tuple[int, int, set[str]]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, AssistantMessage):
            continue
        tool_ids = [block.id for block in message.content if isinstance(block, ToolUseBlock)]
        if not tool_ids:
            continue
        valid_results = {
            tool_id: next(
                (result_index for result_index in result_indexes.get(tool_id, []) if result_index > index),
                None,
            )
            for tool_id in tool_ids
        }
        missing = [tool_id for tool_id, result_index in valid_results.items() if result_index is None]
        end = max([index, *(result_index for result_index in valid_results.values() if result_index is not None)])
        intervals.append((index, end, {"unclosed_tool_call"} if missing else set()))
    intervals.extend((index, index, {"orphan_tool_result"}) for index in orphan_indexes)
    intervals.sort(key=lambda item: (item[0], item[1]))

    merged: list[tuple[int, int, set[str]]] = []
    for start, end, reasons in intervals:
        if merged and start <= merged[-1][1]:
            old_start, old_end, old_reasons = merged[-1]
            merged[-1] = (old_start, max(old_end, end), old_reasons | reasons)
        else:
            merged.append((start, end, set(reasons)))

    groups: list[MessageGroup] = []
    interval_by_start = {start: (end, reasons) for start, end, reasons in merged}
    index = 0
    while index < len(messages):
        end, reasons = interval_by_start.get(index, (index, set()))
        chunk = messages[index : end + 1]
        tool_names = {
            block.name
            for message in chunk
            if isinstance(message, AssistantMessage)
            for block in message.content
            if isinstance(block, ToolUseBlock)
        }
        has_error = any(_message_has_error(message) for message in chunk)
        protected = bool(reasons) or any(
            isinstance(message, (SystemMessage, ContextSummaryMessage))
            for message in chunk
        )
        if protected and not reasons:
            reasons = {"boundary"}
        groups.append(
            MessageGroup(
                start=index,
                end=end,
                messages=chunk,
                tokens=estimate_messages_tokens(chunk),
                protected=protected,
                reasons=set(reasons),
                tool_names=tool_names,
                has_error=has_error,
            )
        )
        index = end + 1
    return groups


async def apply_cached_microcompact(
    messages: list[MessageBase],
    deps: CompactDeps,
) -> list[MessageBase]:
    """为未来 cache editing 保留的 no-op 接口。"""
    _ = deps
    return messages


async def apply_fast_compact(
    messages: list[MessageBase],
    deps: CompactDeps,
) -> list[MessageBase]:
    """快速压缩本期不实现，也不读写 summary.md。"""
    _ = deps
    return messages


def _build_snip_record(
    ui_messages: list[MessageBase],
    projected: list[MessageBase],
    deps: CompactDeps,
    current_tokens: int,
) -> CompactRecordMessage | None:
    groups = build_message_groups(projected)
    _protect_tail(groups, deps.config)
    for index, group in enumerate(groups):
        if group.has_error or any(name.casefold() in EDIT_TOOLS for name in group.tool_names):
            for neighbor in range(max(0, index - 1), min(len(groups), index + 2)):
                groups[neighbor].protected = True
                groups[neighbor].reasons.add("near_edit_or_error")
    desired = max(
        deps.config.snip_min_tokens,
        current_tokens - int(deps.context_window * deps.config.snip_target),
    )
    selected = _select_safe_prefix(
        groups,
        desired_tokens=desired,
        min_messages=deps.config.snip_min_messages,
        min_tokens=deps.config.snip_min_tokens,
    )
    if not selected:
        return None
    covered, superseded = _source_ids_for_groups(selected, ui_messages)
    if not covered:
        return None
    removed_tokens = sum(group.tokens for group in selected)
    summary = (
        "[Snipped earlier conversation segment]\n"
        f"Removed {sum(group.message_count for group in selected)} messages "
        f"(approximately {removed_tokens} tokens)."
    )
    return CompactRecordMessage(
        "snip",
        covered_message_ids=covered,
        superseded_record_ids=superseded,
        summary=summary,
        tokens_before=current_tokens,
        created_step=deps.state.step_index,
        created_turn=deps.state.turn_index,
    )


def _build_time_microcompact_record(
    ui_messages: list[MessageBase],
    projected: list[MessageBase],
    deps: CompactDeps,
    current_tokens: int,
) -> CompactRecordMessage | None:
    config = deps.config
    if not config.time_microcompact_enabled or not deps.is_main_thread:
        return None
    last_assistant = next(
        (message for message in reversed(ui_messages) if isinstance(message, AssistantMessage)),
        None,
    )
    if last_assistant is None:
        return None
    gap_minutes = (time.time() - last_assistant.timestamp) / 60
    if not math.isfinite(gap_minutes) or gap_minutes < config.time_microcompact_gap_minutes:
        return None

    result_ids = {
        block.tool_use_id
        for message in projected
        if isinstance(message, UserMessage)
        for block in message.content
        if isinstance(block, ToolResultBlock)
    }
    compactable_ids = [
        block.id
        for message in projected
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, ToolUseBlock)
        and block.name.casefold() in COMPACTABLE_TOOLS
        and block.id in result_ids
    ]
    keep_recent = max(1, config.time_microcompact_keep_recent)
    clear_ids = compactable_ids[:-keep_recent]
    already_cleared = {
        tool_id
        for record in _active_records(ui_messages)
        if record.compact_type == "time_micro"
        for tool_id in record.cleared_tool_use_ids
    }
    clear_ids = [tool_id for tool_id in clear_ids if tool_id not in already_cleared]
    if not clear_ids:
        return None
    return CompactRecordMessage(
        "time_micro",
        cleared_tool_use_ids=clear_ids,
        tokens_before=current_tokens,
        created_step=deps.state.step_index,
        created_turn=deps.state.turn_index,
    )


async def _build_collapse_record(
    ui_messages: list[MessageBase],
    projected: list[MessageBase],
    deps: CompactDeps,
    current_tokens: int,
) -> CompactRecordMessage | None:
    if deps.summarize is None:
        return None
    groups = build_message_groups(projected)
    _protect_tail(groups, deps.config)
    desired = max(
        deps.config.collapse_min_tokens_saved,
        current_tokens - int(deps.context_window * deps.config.collapse_target),
    )
    selected = _select_safe_prefix(
        groups,
        desired_tokens=desired,
        min_messages=1,
        min_tokens=deps.config.collapse_min_tokens_saved,
    )
    if not selected:
        return None
    candidate = [message for group in selected for message in group.messages]
    candidate_tokens = estimate_messages_tokens(candidate)
    try:
        summary = (await deps.summarize("collapse", _messages_to_summary_text(candidate))).strip()
    except Exception:
        return None
    if not summary or candidate_tokens - estimate_text_tokens(summary) < deps.config.collapse_min_tokens_saved:
        return None
    covered, superseded = _source_ids_for_groups(selected, ui_messages)
    if not covered:
        return None
    return CompactRecordMessage(
        "collapse",
        covered_message_ids=covered,
        superseded_record_ids=superseded,
        summary="[Collapsed context summary]\n" + summary,
        tokens_before=current_tokens,
        created_step=deps.state.step_index,
        created_turn=deps.state.turn_index,
    )


async def _build_auto_record(
    ui_messages: list[MessageBase],
    projected: list[MessageBase],
    deps: CompactDeps,
    current_tokens: int,
    *,
    force: bool,
) -> CompactRecordMessage | None:
    if deps.summarize is None:
        return None
    groups = build_message_groups(projected)
    if not groups:
        return None
    for group in groups:
        if group.protected and group.reasons == {"boundary"} and all(
            isinstance(message, ContextSummaryMessage) for message in group.messages
        ):
            group.protected = False
            group.reasons.clear()
    kept_tokens = 0
    kept_messages = 0
    keep_start = len(groups)
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        kept_tokens += group.tokens
        kept_messages += group.message_count
        keep_start = index
        if kept_tokens >= deps.config.auto_keep_tokens and kept_messages >= deps.config.auto_min_keep_messages:
            break
    if force and keep_start == 0 and len(groups) > deps.config.auto_min_keep_messages:
        keep_start = max(1, len(groups) - deps.config.auto_min_keep_messages)
    candidates = groups[:keep_start]
    while candidates and candidates[0].protected and all(
        isinstance(message, SystemMessage) for message in candidates[0].messages
    ):
        candidates = candidates[1:]
    protected_index = next((index for index, group in enumerate(candidates) if group.protected), None)
    if protected_index is not None:
        candidates = candidates[:protected_index]
    if not candidates:
        return None
    candidate = [message for group in candidates for message in group.messages]
    try:
        summary = (await deps.summarize("auto", _messages_to_summary_text(candidate))).strip()
    except Exception as exc:
        raise RuntimeError("auto compact summary failed") from exc
    if not summary:
        raise RuntimeError("auto compact summary was empty")
    covered, superseded = _source_ids_for_groups(candidates, ui_messages)
    if not covered:
        return None
    return CompactRecordMessage(
        "auto",
        covered_message_ids=covered,
        superseded_record_ids=superseded,
        summary="[Compacted conversation summary]\n" + summary,
        tokens_before=current_tokens,
        created_step=deps.state.step_index,
        created_turn=deps.state.turn_index,
    )


def _select_safe_prefix(
    groups: list[MessageGroup],
    *,
    desired_tokens: int,
    min_messages: int,
    min_tokens: int,
) -> list[MessageGroup]:
    runs: list[list[MessageGroup]] = []
    current: list[MessageGroup] = []
    for group in groups:
        if group.protected:
            if current:
                runs.append(current)
                current = []
            continue
        current.append(group)
    if current:
        runs.append(current)
    for run in runs:
        if sum(group.message_count for group in run) < min_messages:
            continue
        if sum(group.tokens for group in run) < min_tokens:
            continue
        selected: list[MessageGroup] = []
        tokens = 0
        messages = 0
        for group in run:
            selected.append(group)
            tokens += group.tokens
            messages += group.message_count
            if tokens >= desired_tokens and tokens >= min_tokens and messages >= min_messages:
                return selected
        if tokens >= min_tokens and messages >= min_messages:
            return selected
    return []


def _protect_tail(groups: list[MessageGroup], config: CompactConfig) -> None:
    tokens = 0
    messages = 0
    for group in reversed(groups):
        group.protected = True
        group.reasons.add("recent_tail")
        tokens += group.tokens
        messages += group.message_count
        if tokens >= config.protected_tail_tokens and messages >= config.protected_tail_messages:
            break


def _source_ids_for_groups(
    groups: list[MessageGroup],
    ui_messages: list[MessageBase],
) -> tuple[list[str], list[str]]:
    records = {record.uuid: record for record in _active_records(ui_messages)}
    covered: list[str] = []
    superseded: list[str] = []
    for group in groups:
        for message in group.messages:
            if isinstance(message, ContextSummaryMessage) and message.uuid in records:
                record = records[message.uuid]
                covered.extend(record.covered_message_ids)
                superseded.append(record.uuid)
            elif isinstance(message, ContextSummaryMessage):
                covered.append(message.uuid)
            elif isinstance(message, (UserMessage, AssistantMessage)):
                covered.append(message.uuid)
    return _dedupe(covered), _dedupe(superseded)


def _active_records(messages: list[MessageBase]) -> list[CompactRecordMessage]:
    records = [message for message in messages if isinstance(message, CompactRecordMessage)]
    superseded = {
        record_id
        for record in records
        for record_id in record.superseded_record_ids
    }
    return [record for record in records if record.uuid not in superseded]


def _project_tool_result_clears(
    message: MessageBase,
    cleared_tool_ids: set[str],
) -> MessageBase:
    if not isinstance(message, UserMessage) or not cleared_tool_ids:
        return message
    changed = False
    blocks = []
    for block in message.content:
        if isinstance(block, ToolResultBlock) and block.tool_use_id in cleared_tool_ids:
            changed = True
            blocks.append(
                ToolResultBlock(
                    tool_use_id=block.tool_use_id,
                    content=TIME_BASED_CLEARED_MESSAGE,
                    is_error=block.is_error,
                )
            )
        else:
            blocks.append(block)
    if not changed:
        return message
    return UserMessage(
        blocks,
        is_meta=message.is_meta,
        origin=message.origin,
        uuid=message.uuid,
        timestamp=message.timestamp,
    )


def _message_has_error(message: MessageBase) -> bool:
    if not isinstance(message, UserMessage):
        return False
    for block in message.content:
        if not isinstance(block, ToolResultBlock):
            continue
        if block.is_error:
            return True
        text = _json_text(block.content).casefold()
        if any(marker in text for marker in ERROR_MARKERS):
            return True
    return False


def _messages_to_summary_text(messages: list[MessageBase]) -> str:
    lines: list[str] = []
    for message in messages:
        if isinstance(message, UserMessage):
            role = "User"
            blocks = message.content
        elif isinstance(message, AssistantMessage):
            role = "Assistant"
            blocks = message.content
        elif isinstance(message, ContextSummaryMessage):
            lines.append(f"[Prior Summary]\n{message.summary}")
            continue
        else:
            continue
        parts: list[str] = []
        for block in blocks:
            if isinstance(block, TextBlock):
                parts.append(block.text)
            elif isinstance(block, ThinkingBlock):
                parts.append(f"[Thinking] {block.thinking}")
            elif isinstance(block, ToolUseBlock):
                parts.append(f"[Tool Call: {block.name}] {_json_text(block.input)}")
            elif isinstance(block, ToolResultBlock):
                content = _sanitize_summary_content(block.content)
                marker = " ERROR" if block.is_error else ""
                parts.append(f"[Tool Result{marker}: {block.tool_use_id}] {content}")
        if parts:
            lines.append(f"[{role}]\n" + "\n".join(parts))
    return "\n\n".join(lines)


def _sanitize_summary_content(content: object, max_chars: int = 4000) -> str:
    if isinstance(content, dict):
        content_type = str(content.get("type") or "").casefold()
        if content_type == "image":
            return "[image]"
        if content_type in {"document", "attachment", "binary"}:
            return "[attachment]"
    text = _json_text(content)
    if len(text) > max_chars:
        return text[:max_chars] + "\n[truncated]"
    return text


def _estimate_tool_result_tokens(content: object) -> int:
    if isinstance(content, (bytes, bytearray, memoryview)):
        return 2000
    if isinstance(content, dict):
        content_type = str(content.get("type") or "").casefold()
        if content_type in {"image", "document", "attachment", "binary"}:
            return 2000
    return estimate_text_tokens(_json_text(content))


def _json_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _utilization(tokens: int, context_window: int) -> float:
    return tokens / max(1, context_window)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
