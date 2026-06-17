"""Small helpers for tool-level permission checks."""
from __future__ import annotations

from pydantic import BaseModel

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext
from bigcode.tools.permissions import build_permission_target, check_mode_policy_for_target


def allow_with_mode_policy(tool: BaseTool, input: BaseModel, ctx: ToolExecutionContext, message: str) -> PermissionDecision:
    """Return allow unless the current mode/sandbox forbids this tool."""

    target = build_permission_target(tool, input)
    decision = check_mode_policy_for_target(target, ctx, tool)
    if decision:
        return decision
    return PermissionDecision("allow", message=message, updated_input=input)
