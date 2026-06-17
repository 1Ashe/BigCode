from __future__ import annotations

from pydantic import BaseModel, Field

from bigcode.subagents.tasks import task_summary, validate_agent_id
from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permission_helpers import allow_with_mode_policy

from .common import agent_task_store


class TaskOutputInput(BaseModel):
    agent_id: str | None = None
    max_chars: int = Field(default=100_000, ge=0, le=1_000_000)


class TaskOutputTool(BaseTool[TaskOutputInput, dict]):
    name = "TaskOutput"
    description = "List background subAgent tasks or read one task's persisted status and output."
    input_model = TaskOutputInput
    permission_category = "state"
    state_effect = "none"
    max_result_chars = 120_000

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.agent_session is not None or ctx.project_state_dir is not None

    def is_concurrency_safe(self, input: TaskOutputInput, ctx: ToolExecutionContext) -> bool:
        return True

    def is_read_only(self, input: TaskOutputInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: TaskOutputInput, ctx: ToolExecutionContext) -> ValidationResult:
        if ctx.agent_session is None and ctx.project_state_dir is None:
            return ValidationResult(False, "Background subAgent task store is not configured.")
        if input.agent_id is not None:
            try:
                validate_agent_id(input.agent_id)
            except RuntimeError as exc:
                return ValidationResult(False, str(exc))
        return ValidationResult(True)

    async def check_permissions(self, input: TaskOutputInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return allow_with_mode_policy(self, input, ctx, "Background task read allowed.")

    async def call(self, input: TaskOutputInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        store = agent_task_store(ctx)
        if input.agent_id is None:
            states = store.list_states()
            return ToolResult({"count": len(states), "tasks": [task_summary(state) for state in states]})
        agent_id = validate_agent_id(input.agent_id)
        state = store.read_state(agent_id)
        if state is None:
            raise RuntimeError(f"Unknown background subAgent task: {agent_id}")
        output, truncated = store.read_output(agent_id, max_chars=input.max_chars)
        return ToolResult(
            {
                "task": task_summary(state),
                "output": output,
                "truncated": truncated,
                "max_chars": input.max_chars,
            }
        )
