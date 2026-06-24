"""工具层权限辅助函数。"""
from __future__ import annotations

from pydantic import BaseModel

from ..base import BaseTool, PermissionDecision, ToolExecutionContext
from .pipeline import build_permission_target, check_mode_policy_for_target


def allow_with_mode_policy(
    tool: BaseTool,
    input: BaseModel,
    ctx: ToolExecutionContext,
    message: str,
) -> PermissionDecision:
    """Return allow unless the current mode forbids this tool."""

    target = build_permission_target(tool, input)
    decision = check_mode_policy_for_target(target, ctx, tool)
    if decision:
        return decision
    return PermissionDecision("allow", message=message, updated_input=input)
