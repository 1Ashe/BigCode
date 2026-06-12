from __future__ import annotations

from pydantic import BaseModel

from bigcode.tasks.models import TaskStatus
from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy

from .common import task_list_id, task_store


class TaskListInput(BaseModel):
    status: TaskStatus | None = None
    owner: str | None = None
    pending_only: bool = False


class TaskListTool(BaseTool[TaskListInput, dict]):
    name = "TaskList"
    description = "List tasks in the current BigCode task list."
    input_model = TaskListInput
    permission_category = "state"
    state_effect = "app_state"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.task_store is not None

    def is_concurrency_safe(self, input: TaskListInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: TaskListInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not ctx.task_store:
            return ValidationResult(False, "Task store is not configured.")
        return ValidationResult(True)

    async def check_permissions(self, input: TaskListInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "Task read allowed.")

    async def call(self, input: TaskListInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        tasks = task_store(ctx).list(task_list_id(ctx))
        if input.pending_only:
            tasks = [task for task in tasks if task.status != "completed"]
        if input.status:
            tasks = [task for task in tasks if task.status == input.status]
        if input.owner:
            tasks = [task for task in tasks if task.owner == input.owner]
        tasks = [task for task in tasks if not task.metadata.get("_internal")]
        return ToolResult({"tasks": tasks})
