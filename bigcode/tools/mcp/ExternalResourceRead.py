from __future__ import annotations

from pydantic import BaseModel

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult


class ExternalResourceReadInput(BaseModel):
    server: str
    uri: str


class ExternalResourceReadTool(BaseTool[ExternalResourceReadInput, dict]):
    name = "ExternalResourceRead"
    description = "Read an MCP resource from a configured external server."
    input_model = ExternalResourceReadInput
    permission_category = "mcp"
    state_effect = "external"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.mcp_manager is not None

    def is_concurrency_safe(self, input: ExternalResourceReadInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: ExternalResourceReadInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not ctx.mcp_manager:
            return ValidationResult(False, "MCP manager is not configured.")
        if not input.server.strip() or not input.uri.strip():
            return ValidationResult(False, "server and uri must not be empty.")
        return ValidationResult(True)

    async def check_permissions(self, input: ExternalResourceReadInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return PermissionDecision("passthrough", updated_input=input)

    async def call(self, input: ExternalResourceReadInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        if not ctx.mcp_manager:
            raise RuntimeError("MCP manager is not configured.")
        return ToolResult(await ctx.mcp_manager.read_resource(input.server, input.uri))
