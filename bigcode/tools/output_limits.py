"""工具结果截断逻辑。

学习思路：长字符串保留头尾，dict/list 会递归截断，最终用 metadata 标记 truncated。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from .base import ToolResult, ToolRunResult


def limit_text(text: str, max_chars: int) -> tuple[str, bool]:
    """截断长文本，保留开头和结尾，中间插入省略提示。"""
    if len(text) <= max_chars:
        return text, False
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return f"{head}\n\n[... truncated {len(text) - max_chars} chars ...]\n\n{tail}", True


def limit_tool_run_result(result: ToolRunResult[Any], max_chars: int) -> ToolRunResult[Any]:
    """截断 ToolRunResult.output，并在 metadata 中标记 truncated。"""
    if result.output is None:
        return result
    data = result.output.data
    limited, truncated = _limit_value(data, max_chars)
    if not truncated:
        return result
    metadata = dict(result.output.metadata)
    metadata["truncated"] = True
    return replace(result, output=ToolResult(data=limited, metadata=metadata), metadata={**result.metadata, "truncated": True})


def _limit_value(value: Any, max_chars: int) -> tuple[Any, bool]:
    """递归限制任意 JSON 风格值的展示长度。"""
    if isinstance(value, str):
        return limit_text(value, max_chars)
    if isinstance(value, dict):
        changed = False
        out: dict[str, Any] = {}
        remaining = max_chars
        for key, item in value.items():
            # 每个子值至少给 1000 字符预算，避免 remaining 变小后所有字段都被过度截断。
            limited, truncated = _limit_value(item, max(1000, remaining))
            out[key] = limited
            changed = changed or truncated
            remaining -= len(str(limited))
            if remaining <= 0:
                # dict 还有后续字段没放进去时，用特殊键提示调用方结果被截断。
                out["__truncated__"] = True
                return out, True
        return out, changed
    if isinstance(value, list):
        out = []
        changed = False
        remaining = max_chars
        for item in value:
            limited, truncated = _limit_value(item, max(1000, remaining))
            out.append(limited)
            changed = changed or truncated
            remaining -= len(str(limited))
            if remaining <= 0:
                # list 不能加特殊键，所以追加一个标记对象。
                out.append({"__truncated__": True})
                return out, True
        return out, changed
    return value, False
