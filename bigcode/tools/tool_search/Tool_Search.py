"""延迟工具搜索入口。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult


class ToolSearchInput(BaseModel):
    query: str = Field(description="Search keywords, or select:<tool_name>[,<tool_name>...] for exact deferred tool selection.")
    max_results: int = Field(default=5, ge=1, le=20)


class ToolSearchTool(BaseTool[ToolSearchInput, dict]):
    name = "Tool_Search"
    aliases = ("ToolSearch",)
    description = (
        "Search deferred tools and load their full schemas for future tool calls. Use this when you need a "
        "specialized or MCP tool that is mentioned in environment context but not currently callable. Query by "
        "keywords, or use select:<tool_name>[,<tool_name>...] for exact names. After a match is loaded, call the "
        "returned tool by its schema in a later step."
    )
    input_model = ToolSearchInput
    permission_category = "read"
    state_effect = "none"
    always_load = True
    search_hint = "deferred tool discovery search select mcp schema"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return True

    def is_concurrency_safe(self, input: ToolSearchInput, ctx: ToolExecutionContext) -> bool:
        return self.is_read_only(input, ctx)

    def is_read_only(self, input: ToolSearchInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: ToolSearchInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not input.query.strip():
            return ValidationResult(False, "query must not be empty.")
        return ValidationResult(True)

    async def check_permissions(self, input: ToolSearchInput, ctx: ToolExecutionContext) -> PermissionDecision:
        return PermissionDecision("passthrough", updated_input=input)

    async def call(self, input: ToolSearchInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        registry = ctx.tool_registry
        if registry is None:
            raise RuntimeError("Tool registry is not configured.")

        query = input.query.strip()
        total_deferred = len(registry.deferred_tools())
        if query.lower().startswith("select:"):
            names = [name.strip() for name in query.split(":", 1)[1].split(",") if name.strip()]
            tools = registry.find_deferred_by_names(names)
            missing = [name for name in names if registry.get(name) is None]
            mode = "select"
        else:
            matches = registry.search_deferred(query, max_results=input.max_results)
            tools = [match.tool for match in matches]
            missing = []
            mode = "search"

        discovered = registry.mark_discovered_many([tool.name for tool in tools])
        schemas = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.json_schema(),
            }
            for tool in tools
        ]
        return ToolResult(
            {
                "query": query,
                "mode": mode,
                "matches": [tool.name for tool in tools],
                "schemas": schemas,
                "discovered": discovered,
                "missing": missing,
                "total_deferred_tools": total_deferred,
                "all_discovered": sorted(registry.discovered_tool_names()),
            }
        )
