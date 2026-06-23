from __future__ import annotations

from bigcode.hooks.models import HookInput
from bigcode.tools.base import BaseTool, EmptyInput, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy


class EnterPlanModeTool(BaseTool[EmptyInput, dict]):
    name = "EnterPlanMode"
    description = (
        "Enter read-only Plan Mode and create or select the current session plan file. Use this when the user "
        "asks to plan before implementation. After entering, inspect freely and write the plan with the normal "
        "Write/Edit tools at the provided plan_file path. Non-plan writes use normal permission prompts."
    )
    input_model = EmptyInput
    permission_category = "state"
    state_effect = "app_state"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.plan_state is not None and ctx.plan_store is not None

    def is_concurrency_safe(self, input: EmptyInput, ctx: ToolExecutionContext) -> bool:
        return False

    def is_read_only(self, input: EmptyInput, ctx: ToolExecutionContext) -> bool:
        return False

    async def validate_input(self, input: EmptyInput, ctx: ToolExecutionContext) -> ValidationResult:
        if ctx.plan_state is None or ctx.plan_store is None:
            return ValidationResult(False, "Plan state is not configured.")
        return ValidationResult(True)

    async def check_permissions(self, input: EmptyInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "Plan mode state transition allowed.")

    async def call(self, input: EmptyInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        if ctx.plan_state is None or ctx.plan_store is None:
            raise RuntimeError("Plan state is not configured.")
        if ctx.plan_state.active:
            return ToolResult({"active": True, "plan_file": ctx.plan_state.plan_file})
        ctx.plan_state.pre_plan_permission_mode = ctx.permission_context.mode
        ctx.permission_context.mode = "plan"
        ctx.plan_state.active = True
        path = ctx.plan_store.get_path(ctx.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        ctx.plan_state.plan_file = str(path)
        ctx.plan_state.plan_slug = path.stem
        if ctx.hook_bus:
            await ctx.hook_bus.emit(
                "PlanModeEnter",
                HookInput(
                    "PlanModeEnter",
                    ctx.session_id,
                    str(ctx.cwd),
                    ctx.permission_context.mode,
                    payload={"plan_file": str(path)},
                ),
            )
        return ToolResult({"active": True, "plan_file": str(path)})
