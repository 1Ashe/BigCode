from __future__ import annotations

from pydantic import BaseModel

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy

from .common import task_list_id, task_store


class TaskGetInput(BaseModel):
    id: str


class TaskGetTool(BaseTool[TaskGetInput, dict]):
    name = "TaskGet"
    description = "Get one task from the current BigCode task list."
    input_model = TaskGetInput
    permission_category = "state"
    state_effect = "app_state"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.task_store is not None

    def is_concurrency_safe(self, input: TaskGetInput, ctx: ToolExecutionContext) -> bool:
        return True

    def is_read_only(self, input: TaskGetInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: TaskGetInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not ctx.task_store:
            return ValidationResult(False, "Task store is not configured.")
        if not input.id.strip():
            return ValidationResult(False, "id must not be empty.")
        return ValidationResult(True)

    async def check_permissions(self, input: TaskGetInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "Task read allowed.")

    async def call(self, input: TaskGetInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        task = task_store(ctx).get(task_list_id(ctx), input.id)
        if not task:
            raise RuntimeError(f"Task {input.id} does not exist.")
        return ToolResult({"task": task})
