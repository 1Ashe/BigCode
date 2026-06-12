from __future__ import annotations

from pydantic import BaseModel

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult


class ExternalResourceListInput(BaseModel):
    server: str | None = None


class ExternalResourceListTool(BaseTool[ExternalResourceListInput, dict]):
    name = "ExternalResourceList"
    description = "List MCP resources from configured external servers."
    input_model = ExternalResourceListInput
    permission_category = "mcp"
    state_effect = "external"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.mcp_manager is not None

    def is_concurrency_safe(self, input: ExternalResourceListInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: ExternalResourceListInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not ctx.mcp_manager:
            return ValidationResult(False, "MCP manager is not configured.")
        return ValidationResult(True)

    async def check_permissions(self, input: ExternalResourceListInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return PermissionDecision("passthrough", updated_input=input)

    async def call(self, input: ExternalResourceListInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        if not ctx.mcp_manager:
            raise RuntimeError("MCP manager is not configured.")
        return ToolResult({"resources": await ctx.mcp_manager.list_resources(input.server)})
