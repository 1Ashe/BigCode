from __future__ import annotations

from pydantic import BaseModel

from bigcode.hooks.models import HookInput
from bigcode.tasks.models import TaskStatus
from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy

from .common import task_list_id, task_store


class TaskUpdateInput(BaseModel):
    id: str
    status: TaskStatus | None = None
    subject: str | None = None
    description: str | None = None
    active_form: str | None = None
    owner: str | None = None
    clear_owner: bool = False
    blocks: list[str] | None = None
    blocked_by: list[str] | None = None


class TaskUpdateTool(BaseTool[TaskUpdateInput, dict]):
    name = "TaskUpdate"
    description = (
        "Update an existing task in the current BigCode task list. Use this to change status, owner, subject, "
        "details, or metadata after observed progress. Do not mark work complete until it has been verified. "
        "This changes app task state."
    )
    input_model = TaskUpdateInput
    permission_category = "state"
    state_effect = "app_state"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.task_store is not None

    def is_concurrency_safe(self, input: TaskUpdateInput, ctx: ToolExecutionContext) -> bool:
        return False

    def is_read_only(self, input: TaskUpdateInput, ctx: ToolExecutionContext) -> bool:
        return False

    async def validate_input(self, input: TaskUpdateInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not ctx.task_store:
            return ValidationResult(False, "Task store is not configured.")
        if not input.id.strip():
            return ValidationResult(False, "id must not be empty.")
        return ValidationResult(True)

    async def check_permissions(self, input: TaskUpdateInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "Task state update allowed.")

    async def call(self, input: TaskUpdateInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        list_id = task_list_id(ctx)
        store = task_store(ctx)
        previous = store.get(list_id, input.id)
        task = store.update(list_id, input.id, input)
        if not task:
            raise RuntimeError(f"Task {input.id} does not exist.")
        if ctx.hook_bus:
            await ctx.hook_bus.emit(
                "TaskUpdated",
                HookInput(
                    "TaskUpdated",
                    ctx.session_id,
                    str(ctx.cwd),
                    ctx.permission_context.mode,
                    payload={"task": task, "previous_task": previous, "task_list_id": list_id},
                ),
            )
        return ToolResult({"task": task})
