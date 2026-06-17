from __future__ import annotations

from pydantic import BaseModel

from bigcode.hooks.models import HookInput
from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy

from .common import task_list_id, task_store


class TaskBlockInput(BaseModel):
    from_task_id: str
    to_task_id: str


class TaskBlockTool(BaseTool[TaskBlockInput, dict]):
    name = "TaskBlock"
    aliases = ("TaskBlockTask",)
    description = "Record that one task blocks another task."
    input_model = TaskBlockInput
    permission_category = "state"
    state_effect = "app_state"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.task_store is not None

    def is_concurrency_safe(self, input: TaskBlockInput, ctx: ToolExecutionContext) -> bool:
        return False

    def is_read_only(self, input: TaskBlockInput, ctx: ToolExecutionContext) -> bool:
        return False

    async def validate_input(self, input: TaskBlockInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not ctx.task_store:
            return ValidationResult(False, "Task store is not configured.")
        if not input.from_task_id.strip() or not input.to_task_id.strip():
            return ValidationResult(False, "task ids must not be empty.")
        return ValidationResult(True)

    async def check_permissions(self, input: TaskBlockInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "Task state update allowed.")

    async def call(self, input: TaskBlockInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        list_id = task_list_id(ctx)
        store = task_store(ctx)
        previous_target = store.get(list_id, input.to_task_id)
        source, target = store.block_task(list_id, input.from_task_id, input.to_task_id)
        if ctx.hook_bus:
            await ctx.hook_bus.emit(
                "TaskUpdated",
                HookInput(
                    "TaskUpdated",
                    ctx.session_id,
                    str(ctx.cwd),
                    ctx.permission_context.mode,
                    payload={"task": target, "previous_task": previous_target, "task_list_id": list_id},
                ),
            )
        return ToolResult({"from_task": source, "to_task": target})
