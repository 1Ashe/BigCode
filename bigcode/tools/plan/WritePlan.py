from __future__ import annotations

from pydantic import BaseModel

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy


class WritePlanInput(BaseModel):
    content: str


class WritePlanTool(BaseTool[WritePlanInput, dict]):
    name = "WritePlan"
    description = "Overwrite the current session plan file. This is the only write allowed in Plan Mode."
    input_model = WritePlanInput
    permission_category = "state"
    state_effect = "app_state"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.plan_state is not None and ctx.plan_store is not None

    def is_concurrency_safe(self, input: WritePlanInput, ctx: ToolExecutionContext) -> bool:
        return False

    def is_read_only(self, input: WritePlanInput, ctx: ToolExecutionContext) -> bool:
        return False

    async def validate_input(self, input: WritePlanInput, ctx: ToolExecutionContext) -> ValidationResult:
        if ctx.plan_state is None or ctx.plan_store is None or not ctx.plan_state.active:
            return ValidationResult(False, "WritePlan requires active Plan Mode.")
        return ValidationResult(True)

    async def check_permissions(self, input: WritePlanInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "Plan file write allowed.")

    async def call(self, input: WritePlanInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        if ctx.plan_state is None or ctx.plan_store is None or not ctx.plan_state.active:
            raise RuntimeError("WritePlan requires active Plan Mode.")
        path = ctx.plan_store.write(ctx.session_id, input.content)
        ctx.plan_state.plan_file = str(path)
        return ToolResult({"path": str(path), "chars": len(input.content)})
