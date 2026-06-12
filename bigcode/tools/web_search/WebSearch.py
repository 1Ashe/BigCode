"""占位版网页搜索工具。

学习思路：当前没有真实搜索后端，只返回说明；真实搜索可以通过 MCP 或后续实现接入。
"""
from __future__ import annotations

from pydantic import BaseModel

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permissions import build_permission_target, check_content_policy


class WebSearchInput(BaseModel):
    """工具输入模型。

    Pydantic 会根据这些字段校验模型传进来的参数，字段类型就是最重要的阅读线索。
    """
    query: str


class WebSearchTool(BaseTool[WebSearchInput, dict]):
    """一个可被模型调用的工具类。

    ToolRunner 会先校验 input_model 和权限，再调用这个类的 call() 方法执行真正逻辑。
    """
    name = "WebSearch"
    description = "Placeholder web search tool. Configure an MCP/search backend for real search."
    input_model = WebSearchInput
    permission_category = "network"
    state_effect = "external"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return True

    def is_concurrency_safe(self, input: WebSearchInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: WebSearchInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not input.query.strip():
            return ValidationResult(False, "query must not be empty.")
        return ValidationResult(True)

    async def check_permissions(self, input: WebSearchInput, ctx: ToolExecutionContext) -> PermissionDecision:
        target = build_permission_target(self, input)
        decision = check_content_policy(target, ctx)
        if decision:
            return decision
        return PermissionDecision("passthrough", updated_input=input)

    async def call(self, input: WebSearchInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        """工具执行入口。

        input 是已经通过 Pydantic 校验的参数，ctx 提供 cwd、权限、会话状态等运行环境。
        """
        return ToolResult({"query": input.query, "results": [], "message": "WebSearch backend is not configured in BigCode v1."})
