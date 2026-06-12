from __future__ import annotations

from pydantic import BaseModel

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult


class ExternalPromptListInput(BaseModel):
    server: str | None = None


class ExternalPromptListTool(BaseTool[ExternalPromptListInput, dict]):
    name = "ExternalPromptList"
    description = "List MCP prompts from configured external servers."
    input_model = ExternalPromptListInput
    permission_category = "mcp"
    state_effect = "external"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.mcp_manager is not None

    def is_concurrency_safe(self, input: ExternalPromptListInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: ExternalPromptListInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not ctx.mcp_manager:
            return ValidationResult(False, "MCP manager is not configured.")
        return ValidationResult(True)

    async def check_permissions(self, input: ExternalPromptListInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return PermissionDecision("passthrough", updated_input=input)

    async def call(self, input: ExternalPromptListInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        if not ctx.mcp_manager:
            raise RuntimeError("MCP manager is not configured.")
        return ToolResult({"prompts": await ctx.mcp_manager.list_prompts(input.server)})
