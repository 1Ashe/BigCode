from __future__ import annotations

from pydantic import BaseModel

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult


class ExternalPromptGetInput(BaseModel):
    server: str
    name: str
    arguments: dict | None = None


class ExternalPromptGetTool(BaseTool[ExternalPromptGetInput, dict]):
    name = "ExternalPromptGet"
    description = "Get an MCP prompt from a configured external server."
    input_model = ExternalPromptGetInput
    permission_category = "mcp"
    state_effect = "external"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.mcp_manager is not None

    def is_concurrency_safe(self, input: ExternalPromptGetInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: ExternalPromptGetInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not ctx.mcp_manager:
            return ValidationResult(False, "MCP manager is not configured.")
        if not input.server.strip() or not input.name.strip():
            return ValidationResult(False, "server and name must not be empty.")
        return ValidationResult(True)

    async def check_permissions(self, input: ExternalPromptGetInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return PermissionDecision("passthrough", updated_input=input)

    async def call(self, input: ExternalPromptGetInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        if not ctx.mcp_manager:
            raise RuntimeError("MCP manager is not configured.")
        return ToolResult(await ctx.mcp_manager.get_prompt(input.server, input.name, input.arguments or {}))
