from __future__ import annotations

import asyncio

from pydantic import BaseModel

from bigcode.hooks.models import HookInput
from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy


class ExitPlanModeInput(BaseModel):
    allowed_prompts: list[dict] | None = None


class ExitPlanModeTool(BaseTool[ExitPlanModeInput, dict]):
    name = "ExitPlanMode"
    description = "Submit the current plan for user approval and leave Plan Mode if approved."
    input_model = ExitPlanModeInput
    permission_category = "state"
    state_effect = "app_state"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.plan_state is not None and ctx.plan_store is not None

    def is_concurrency_safe(self, input: ExitPlanModeInput, ctx: ToolExecutionContext) -> bool:
        return False

    async def validate_input(self, input: ExitPlanModeInput, ctx: ToolExecutionContext) -> ValidationResult:
        if ctx.plan_state is None or ctx.plan_store is None or not ctx.plan_state.active:
            return ValidationResult(False, "ExitPlanMode requires active Plan Mode.")
        plan = ctx.plan_store.read(ctx.session_id) or ""
        if not plan.strip():
            return ValidationResult(False, "Plan file is empty; write a plan before exiting.")
        return ValidationResult(True)

    async def check_permissions(self, input: ExitPlanModeInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "Plan approval request allowed.")

    async def call(self, input: ExitPlanModeInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        if ctx.plan_state is None or ctx.plan_store is None or not ctx.plan_state.active:
            raise RuntimeError("ExitPlanMode requires active Plan Mode.")
        plan = ctx.plan_store.read(ctx.session_id) or ""
        if not plan.strip():
            raise RuntimeError("Plan file is empty; write a plan before exiting.")
        if ctx.is_non_interactive_session:
            return ToolResult({"requires_approval": True, "plan": plan})
        approved = await asyncio.to_thread(_approve_plan, plan)
        if not approved:
            return ToolResult({"approved": False, "active": True})
        ctx.plan_state.approved_plan = plan
        ctx.plan_state.active = False
        ctx.plan_state.has_exited_plan_mode = True
        ctx.plan_state.needs_exit_attachment = True
        ctx.permission_context.mode = ctx.plan_state.pre_plan_permission_mode or "default"
        if ctx.hook_bus:
            await ctx.hook_bus.emit(
                "PlanModeExit",
                HookInput(
                    "PlanModeExit",
                    ctx.session_id,
                    str(ctx.cwd),
                    ctx.permission_context.mode,
                    payload={"approved_plan": plan, "approval_result": "approved"},
                ),
            )
        return ToolResult({"approved": True, "active": False})


def _approve_plan(plan: str) -> bool:
    print("\n--- Plan Approval Request ---")
    print(plan)
    print("--- End Plan ---")
    return input("Approve this plan? [y/N] ").strip().lower() in {"y", "yes"}
