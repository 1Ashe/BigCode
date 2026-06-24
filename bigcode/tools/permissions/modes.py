"""权限模式矩阵与通用默认策略。"""
from __future__ import annotations

from pathlib import Path

from ..base import BaseTool, PermissionDecision, ToolExecutionContext
from .models import PermissionTarget
from .safety import _inside_any, _resolve_existing_or_parent, classify_bash


# ---------------------------------------------------------------------------
# Mode × Category → default effect matrix
# "allow-workspace" means allow only when path is inside a workspace root.
# ---------------------------------------------------------------------------

_MODE_MATRIX: dict[tuple[str, str], str] = {
    # -- default --
    ("default", "read"):     "allow",
    ("default", "write"):    "ask",
    ("default", "edit"):     "ask",
    ("default", "delete"):   "ask",
    ("default", "bash"):     "ask",
    ("default", "network"):  "ask",
    ("default", "agent"):    "ask",
    ("default", "mcp"):      "ask",
    ("default", "skill"):    "allow",
    ("default", "state"):    "allow",

    # -- acceptEdits --
    ("acceptEdits", "read"):     "allow",
    ("acceptEdits", "write"):    "allow-workspace",
    ("acceptEdits", "edit"):     "allow-workspace",
    ("acceptEdits", "delete"):   "ask",
    ("acceptEdits", "bash"):     "ask",
    ("acceptEdits", "network"):  "ask",
    ("acceptEdits", "agent"):    "ask",
    ("acceptEdits", "mcp"):      "ask",
    ("acceptEdits", "skill"):    "allow",
    ("acceptEdits", "state"):    "allow",

    # -- plan --
    ("plan", "read"):     "allow-workspace",
    ("plan", "write"):    "ask",
    ("plan", "edit"):     "ask",
    ("plan", "delete"):   "ask",
    ("plan", "bash"):     "ask",
    ("plan", "network"):  "ask",
    ("plan", "agent"):    "ask",
    ("plan", "mcp"):      "ask",
    ("plan", "skill"):    "allow",
    ("plan", "state"):    "allow",
}


def _apply_generic_defaults(
    decision: PermissionDecision,
    tool: BaseTool,
    target: PermissionTarget,
    ctx: ToolExecutionContext,
) -> PermissionDecision:
    """把 passthrough 结果交给通用权限层补足。

    采用「矩阵查基本值 + overlay」模式：
    1. plan-file 写入直接 allow（覆盖矩阵）
    2. 只读 Bash 自动 allow（覆盖矩阵）
    3. plan 模式下的只读工具自动 allow（覆盖矩阵）
    4. 矩阵查表得到基本 effect，再解析 workspace-bounded 结果
    """
    if decision.behavior != "passthrough":
        return decision

    mode = ctx.permission_context.mode
    category = target.category

    # -- Overlay 1: plan-file 写入 --
    if mode == "plan" and _is_plan_file_write(target, ctx):
        return PermissionDecision(
            "allow", message="Plan file write allowed in Plan Mode.",
            updated_input=target.raw, decision_reason={"type": "mode"},
        )

    # -- Overlay 2: 只读 Bash 自动放行 --
    if category == "bash" and target.command and classify_bash(target.command) == "read":
        return PermissionDecision(
            "allow", message="Read-only Bash allowed.",
            updated_input=target.raw,
        )

    # -- Overlay 3: plan 模式下的只读工具 --
    if mode == "plan" and _read_only_allowed_in_restricted_mode(tool, target, ctx):
        return PermissionDecision(
            "allow", message="Read-only tool allowed in Plan Mode.",
            updated_input=target.raw, decision_reason={"type": "mode"},
        )

    # -- Primary lookup: mode × category matrix --
    effect = _MODE_MATRIX.get((mode, category))
    if effect is None:
        return PermissionDecision(
            "deny",
            message=f"Unknown permission category {category!r}.",
            updated_input=target.raw,
        )

    # -- Resolve workspace-bounded entries --
    if effect == "allow-workspace" and target.path is not None:
        resolved = _resolve_existing_or_parent(ctx.cwd, target.path)
        if _inside_any(resolved, ctx.workspace_roots):
            effect = "allow"
        else:
            effect = "ask"

    # -- Read without path: always allow --
    if effect == "allow-workspace" and target.path is None:
        effect = "allow"

    if effect == "allow":
        return PermissionDecision("allow", message=f"{tool.name} allowed.", updated_input=target.raw)
    return PermissionDecision(
        "ask", message=f"{tool.name} requires permission.",
        updated_input=target.raw,
    )


# ---------------------------------------------------------------------------
# Plan-mode helpers
# ---------------------------------------------------------------------------

def _is_plan_file_write(target: PermissionTarget, ctx: ToolExecutionContext) -> bool:
    """Return True when Write/Edit targets the active Plan Mode file."""

    if target.tool_name not in {"Write", "Edit"} or target.path is None:
        return False
    state = getattr(ctx, "plan_state", None)
    plan_file = getattr(state, "plan_file", None)
    if not plan_file:
        return False
    try:
        target_resolved = _resolve_existing_or_parent(ctx.cwd, target.path)
        plan_resolved = _resolve_existing_or_parent(ctx.cwd, Path(plan_file))
    except Exception:
        return False
    return target_resolved == plan_resolved


def _read_only_allowed_in_restricted_mode(
    tool: BaseTool | None, target: PermissionTarget, ctx: ToolExecutionContext
) -> bool:
    """Plan/read-only sandbox 中使用 ReadOnly，但不把网络工具纳入静默放行。"""

    if target.category in {"write", "edit", "delete", "network"}:
        return False
    return _target_is_read_only(tool, target, ctx)


def _target_is_read_only(
    tool: BaseTool | None, target: PermissionTarget, ctx: ToolExecutionContext
) -> bool:
    if tool is None and ctx.tool_registry is not None:
        candidate = ctx.tool_registry.get(target.tool_name)
        if isinstance(candidate, BaseTool):
            tool = candidate
    if tool is None or target.raw is None:
        return False
    try:
        return bool(tool.is_read_only(target.raw, ctx))
    except Exception:
        return False
