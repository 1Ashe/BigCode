from __future__ import annotations

from pydantic import BaseModel

from bigcode.hooks.models import HookInput
from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy

from .common import task_list_id, task_store


class TaskClaimInput(BaseModel):
    id: str
    owner: str
    check_busy: bool = False


class TaskClaimTool(BaseTool[TaskClaimInput, dict]):
    name = "TaskClaim"
    description = (
        "Atomically claim a pending, unblocked task for an owner. Use this when multiple agents or workers may "
        "pull from the same task list and you need exclusive ownership before starting work. This changes app "
        "task state."
    )
    input_model = TaskClaimInput
    permission_category = "state"
    state_effect = "app_state"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.task_store is not None

    def is_concurrency_safe(self, input: TaskClaimInput, ctx: ToolExecutionContext) -> bool:
        return False

    def is_read_only(self, input: TaskClaimInput, ctx: ToolExecutionContext) -> bool:
        return False

    async def validate_input(self, input: TaskClaimInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not ctx.task_store:
            return ValidationResult(False, "Task store is not configured.")
        if not input.id.strip() or not input.owner.strip():
            return ValidationResult(False, "id and owner must not be empty.")
        return ValidationResult(True)

    async def check_permissions(self, input: TaskClaimInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "Task state update allowed.")

    async def call(self, input: TaskClaimInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        list_id = task_list_id(ctx)
        store = task_store(ctx)
        previous = store.get(list_id, input.id)
        result = store.claim(list_id, input.id, input.owner, input.check_busy)
        if not result.claimed:
            raise RuntimeError(result.reason)
        if ctx.hook_bus:
            await ctx.hook_bus.emit(
                "TaskUpdated",
                HookInput(
                    "TaskUpdated",
                    ctx.session_id,
                    str(ctx.cwd),
                    ctx.permission_context.mode,
                    payload={"task": result.task, "previous_task": previous, "task_list_id": list_id},
                ),
            )
        return ToolResult({"claimed": True, "task": result.task})
