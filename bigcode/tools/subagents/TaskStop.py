from __future__ import annotations

from pydantic import BaseModel

from bigcode.subagents.tasks import task_summary, validate_agent_id
from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy

from .common import agent_task_store


class TaskStopInput(BaseModel):
    agent_id: str


class TaskStopTool(BaseTool[TaskStopInput, dict]):
    name = "TaskStop"
    description = "Cancel an in-process background subAgent task if it is still running."
    input_model = TaskStopInput
    permission_category = "state"
    state_effect = "app_state"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.agent_session is not None or ctx.project_state_dir is not None

    def is_concurrency_safe(self, input: TaskStopInput, ctx: ToolExecutionContext) -> bool:
        return False

    async def validate_input(self, input: TaskStopInput, ctx: ToolExecutionContext) -> ValidationResult:
        if ctx.agent_session is None and ctx.project_state_dir is None:
            return ValidationResult(False, "Background subAgent task store is not configured.")
        try:
            validate_agent_id(input.agent_id)
        except RuntimeError as exc:
            return ValidationResult(False, str(exc))
        return ValidationResult(True)

    async def check_permissions(self, input: TaskStopInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "Background task state update allowed.")

    async def call(self, input: TaskStopInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        agent_id = validate_agent_id(input.agent_id)
        if ctx.agent_session is not None and hasattr(ctx.agent_session, "cancel_background_subagent"):
            result = await ctx.agent_session.cancel_background_subagent(agent_id)
            task = result.get("task")
            return ToolResult({"status": result["status"], "task": task_summary(task) if task else None})
        store = agent_task_store(ctx)
        state = store.read_state(agent_id)
        if state is not None:
            return ToolResult({"status": "not_running", "task": task_summary(state)})
        raise RuntimeError(f"Unknown background subAgent task: {agent_id}")
