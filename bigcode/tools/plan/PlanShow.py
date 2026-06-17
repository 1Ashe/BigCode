from __future__ import annotations

from bigcode.tools.base import BaseTool, EmptyInput, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy


class PlanShowTool(BaseTool[EmptyInput, dict]):
    name = "PlanShow"
    description = "Show the current plan file path and content."
    input_model = EmptyInput
    permission_category = "state"
    state_effect = "app_state"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.plan_store is not None

    def is_concurrency_safe(self, input: EmptyInput, ctx: ToolExecutionContext) -> bool:
        return True

    def is_read_only(self, input: EmptyInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: EmptyInput, ctx: ToolExecutionContext) -> ValidationResult:
        if ctx.plan_store is None:
            return ValidationResult(False, "Plan store is not configured.")
        return ValidationResult(True)

    async def check_permissions(self, input: EmptyInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "Plan read allowed.")

    async def call(self, input: EmptyInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        if ctx.plan_store is None:
            raise RuntimeError("Plan store is not configured.")
        path = ctx.plan_store.get_path(ctx.session_id)
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        return ToolResult({"path": str(path), "content": content})
