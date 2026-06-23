from __future__ import annotations

from pydantic import BaseModel, Field

from bigcode.subagents.definitions import builtin_agent_map
from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permissions import build_permission_target, check_content_policy, check_mode_policy_for_target


class AgentToolInput(BaseModel):
    prompt: str
    subagent_type: str | None = Field(default="general-purpose")
    description: str | None = None
    model: str | None = None
    background: bool = False
    run_in_background: bool | None = False


class AgentTool(BaseTool[AgentToolInput, dict]):
    name = "Agent"
    description = (
        "Run a built-in subAgent synchronously or as an in-process background task. Use this to delegate complex "
        "exploration, planning, implementation, or verification when a focused sub-agent can work independently. "
        "Provide the subagent_type and a complete prompt with scope, constraints, and expected output. Background "
        "runs return an id; use TaskOutput to inspect results."
    )
    input_model = AgentToolInput
    permission_category = "agent"
    state_effect = "external"
    max_result_chars = 100_000

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return True

    def is_concurrency_safe(self, input: AgentToolInput, ctx: ToolExecutionContext) -> bool:
        return False

    def is_read_only(self, input: AgentToolInput, ctx: ToolExecutionContext) -> bool:
        subagent_type = input.subagent_type or "general-purpose"
        return subagent_type in {"explorer", "planAgent"} and not (input.background or input.run_in_background)

    async def validate_input(self, input: AgentToolInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not input.prompt.strip():
            return ValidationResult(False, "prompt must not be empty.")
        subagent_type = input.subagent_type or "general-purpose"
        if subagent_type not in builtin_agent_map():
            available = ", ".join(sorted(builtin_agent_map()))
            return ValidationResult(False, f"Unknown subAgent type: {subagent_type}. Available types: {available}")
        return ValidationResult(True)

    async def check_permissions(self, input: AgentToolInput, ctx: ToolExecutionContext) -> PermissionDecision:
        target = build_permission_target(self, input)
        decision = check_content_policy(target, ctx)
        if decision:
            return decision
        decision = check_mode_policy_for_target(target, ctx, self)
        if decision:
            return decision
        return PermissionDecision("passthrough", updated_input=input)

    async def call(self, input: AgentToolInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        agents = builtin_agent_map()
        subagent_type = input.subagent_type or "general-purpose"
        definition = agents.get(subagent_type)
        if not definition:
            available = ", ".join(sorted(agents))
            raise RuntimeError(f"Unknown subAgent type: {subagent_type}. Available types: {available}")
        if ctx.agent_session is None:
            return ToolResult(
                {
                    "status": "unavailable",
                    "agent": definition.name,
                    "result": "Agent session is not available; run this task in the main session.",
                }
            )
        description = _description_for(input, definition.description)
        if input.background or input.run_in_background or definition.background:
            state = ctx.agent_session.start_background_subagent(definition, input.prompt, description=description, model_ref=input.model)
            return ToolResult(
                {
                    "status": "async_launched",
                    "agent_id": state.agent_id,
                    "agent_type": definition.name,
                    "description": state.description,
                    "prompt": state.prompt,
                    "output_file": state.output_file,
                }
            )
        result = await ctx.agent_session.run_subagent(definition, input.prompt, model_ref=input.model)
        return ToolResult(
            {
                "status": "completed",
                "agent_id": result.agent_id,
                "agent_type": result.agent_type,
                "agent": result.agent_type,
                "content": result.content,
                "result": result.content,
                "total_tool_use_count": result.total_tool_use_count,
                "total_duration_ms": result.total_duration_ms,
                "total_tokens": result.total_tokens,
                "stop_reason": result.stop_reason,
            }
        )


def _description_for(input: AgentToolInput, fallback: str) -> str:
    if input.description and input.description.strip():
        return input.description.strip()[:120]
    for line in input.prompt.splitlines():
        text = line.strip()
        if text:
            return text[:120]
    return fallback[:120]
