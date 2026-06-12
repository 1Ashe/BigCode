from __future__ import annotations

from pydantic import BaseModel

from bigcode.hooks.models import HookInput
from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy

from .common import task_list_id, task_store


class TaskCreateInput(BaseModel):
    subject: str
    description: str
    active_form: str | None = None
    metadata: dict | None = None


class TaskCreateTool(BaseTool[TaskCreateInput, dict]):
    name = "TaskCreate"
    description = "Create a task in the current BigCode task list."
    input_model = TaskCreateInput
    permission_category = "state"
    state_effect = "app_state"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.task_store is not None

    def is_concurrency_safe(self, input: TaskCreateInput, ctx: ToolExecutionContext) -> bool:
        return False

    async def validate_input(self, input: TaskCreateInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not ctx.task_store:
            return ValidationResult(False, "Task store is not configured.")
        if not input.subject.strip():
            return ValidationResult(False, "subject must not be empty.")
        return ValidationResult(True)

    async def check_permissions(self, input: TaskCreateInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "Task state update allowed.")

    async def call(self, input: TaskCreateInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        list_id = task_list_id(ctx)
        store = task_store(ctx)
        task_id = store.create(list_id, input)
        task = store.get(list_id, task_id)
        if ctx.hook_bus:
            await ctx.hook_bus.emit(
                "TaskCreated",
                HookInput("TaskCreated", ctx.session_id, str(ctx.cwd), ctx.permission_context.mode, payload={"task": task, "task_list_id": list_id}),
            )
        return ToolResult({"task": {"id": task_id, "subject": input.subject}})
