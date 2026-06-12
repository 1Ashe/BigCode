"""工具注册表。

学习思路：ToolRegistry 负责按名称/别名查工具，并把所有工具的 JSON schema 提供给模型。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import BaseTool


@dataclass(frozen=True)
class ToolRoute:
    """工具路由信息。

    当前大多是 local，保留 metadata 是为了未来支持外部工具来源。
    """
    kind: str = "local"
    metadata: dict[str, Any] | None = None


class ToolRegistry:
    """工具名称到工具实例的映射表。

    它同时处理别名，并提供模型需要的工具 schema 列表。
    """
    def __init__(self) -> None:
        """初始化工具名表和路由表。"""
        self._tools: dict[str, BaseTool] = {}
        self._routes: dict[str, ToolRoute] = {}

    def register(self, tool: BaseTool, *, route: ToolRoute | None = None) -> None:
        """注册工具主名称和所有别名。"""
        if not getattr(tool, "name", None):
            raise ValueError("tool must declare name")
        if not getattr(tool, "permission_category", None):
            raise ValueError(f"tool {tool.name} must declare permission_category")
        self._register_name(tool.name, tool, route)
        for alias in tool.aliases:
            self._register_name(alias, tool, route)

    def _register_name(self, name: str, tool: BaseTool, route: ToolRoute | None) -> None:
        """注册单个工具名或别名，并拒绝重复名称。"""
        if name in self._tools:
            raise ValueError(f"duplicate tool name {name}")
        self._tools[name] = tool
        self._routes[name] = route or ToolRoute()

    def get(self, name: str) -> BaseTool | None:
        """按名称或别名取工具实例。"""
        return self._tools.get(name)

    def route_for(self, name: str) -> ToolRoute | None:
        """查询某个工具名对应的路由信息。"""
        return self._routes.get(name)

    def list_tools(self) -> list[BaseTool]:
        """返回去重后的工具实例列表。"""
        seen: set[int] = set()
        out: list[BaseTool] = []
        for tool in self._tools.values():
            if id(tool) in seen:
                continue
            seen.add(id(tool))
            out.append(tool)
        return out

    def schemas_for_model(self) -> list[dict[str, Any]]:
        """把所有去重后的工具转换成模型 API 需要的 schema 列表。"""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.json_schema(),
            }
            for tool in self.list_tools()
        ]


def build_default_registry() -> ToolRegistry:
    """创建 BigCode 默认工具注册表。"""
    # 这些 import 放在函数里面，是为了避免模块导入阶段形成循环依赖。
    # 只有真正构建默认 registry 时，才需要把所有工具类都加载进来。
    from .bash.Bash import BashTool
    from .edit.Edit import EditTool
    from .glob.Glob import GlobTool
    from .grep.Grep import GrepTool
    from .mcp.ExternalPromptGet import ExternalPromptGetTool
    from .mcp.ExternalPromptList import ExternalPromptListTool
    from .mcp.ExternalResourceList import ExternalResourceListTool
    from .mcp.ExternalResourceRead import ExternalResourceReadTool
    from .plan.AskUserQuestion import AskUserQuestionTool
    from .plan.EnterPlanMode import EnterPlanModeTool
    from .plan.ExitPlanMode import ExitPlanModeTool
    from .plan.PlanShow import PlanShowTool
    from .plan.WritePlan import WritePlanTool
    from .read.Read import ReadTool
    from .skills.SkillLoad import SkillLoadTool
    from .skills.SkillResourceRead import SkillResourceReadTool
    from .subagents.Agent import AgentTool
    from .subagents.TaskOutput import TaskOutputTool
    from .subagents.TaskStop import TaskStopTool
    from .tasks.TaskBlock import TaskBlockTool
    from .tasks.TaskClaim import TaskClaimTool
    from .tasks.TaskCreate import TaskCreateTool
    from .tasks.TaskGet import TaskGetTool
    from .tasks.TaskList import TaskListTool
    from .tasks.TaskUpdate import TaskUpdateTool
    from .web_fetch.WebFetch import WebFetchTool
    from .web_search.WebSearch import WebSearchTool
    from .write.Write import WriteTool

    registry = ToolRegistry()

    # 这个列表就是模型默认能看到的工具面。注册时 ToolRegistry 会同时注册别名，
    # 例如 ReadTool.aliases 里的 ReadFile。
    for tool in [
        ReadTool(),
        EditTool(),
        WriteTool(),
        GlobTool(),
        GrepTool(),
        BashTool(),
        WebFetchTool(),
        WebSearchTool(),
        TaskCreateTool(),
        TaskUpdateTool(),
        TaskListTool(),
        TaskGetTool(),
        TaskClaimTool(),
        TaskBlockTool(),
        EnterPlanModeTool(),
        WritePlanTool(),
        PlanShowTool(),
        ExitPlanModeTool(),
        AskUserQuestionTool(),
        SkillLoadTool(),
        SkillResourceReadTool(),
        ExternalResourceListTool(),
        ExternalResourceReadTool(),
        ExternalPromptListTool(),
        ExternalPromptGetTool(),
        AgentTool(),
        TaskOutputTool(),
        TaskStopTool(),
    ]:
        registry.register(tool)
    return registry
