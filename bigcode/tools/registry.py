"""工具注册表。

学习思路：ToolRegistry 负责按名称/别名查工具，并把所有工具的 JSON schema 提供给模型。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .base import BaseTool


@dataclass(frozen=True)
class ToolRoute:
    """工具路由信息。

    当前大多是 local，保留 metadata 是为了未来支持外部工具来源。
    """
    kind: str = "local"
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolSearchMatch:
    """延迟工具搜索结果。"""

    tool: BaseTool
    score: int
    reasons: tuple[str, ...] = ()


class ToolRegistry:
    """工具名称到工具实例的映射表。

    它同时处理别名，并提供模型需要的工具 schema 列表。
    """
    def __init__(self) -> None:
        """初始化工具名表和路由表。"""
        self._tools: dict[str, BaseTool] = {}
        self._routes: dict[str, ToolRoute] = {}
        self._discovered: set[str] = set()

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

    def is_deferred(self, tool: BaseTool) -> bool:
        """判断工具是否默认延迟暴露给模型。"""
        if getattr(tool, "always_load", False):
            return False
        if tool.name == "Tool_Search":
            return False
        if getattr(tool, "is_mcp", False):
            return True
        return bool(getattr(tool, "should_defer", False))

    def deferred_tools(self) -> list[BaseTool]:
        """返回所有延迟工具，不区分是否已经被发现。"""
        return [tool for tool in self.list_tools() if self.is_deferred(tool)]

    def discovered_tool_names(self) -> set[str]:
        """返回已经通过 Tool_Search 发现的规范工具名。"""
        return set(self._discovered)

    def mark_discovered(self, name: str) -> bool:
        """把延迟工具标记为已发现；name 可以是主名称或别名。"""
        tool = self.get(name)
        if tool is None or not self.is_deferred(tool):
            return False
        self._discovered.add(tool.name)
        return True

    def mark_discovered_many(self, names: list[str]) -> list[str]:
        """批量标记已发现工具，并返回成功发现的规范工具名。"""
        discovered: list[str] = []
        for name in names:
            tool = self.get(name)
            if tool is None:
                continue
            if self.mark_discovered(name):
                discovered.append(tool.name)
        return _dedupe(discovered)

    def inherit_discoveries_from(self, other: "ToolRegistry") -> None:
        """复制另一个 registry 中当前 registry 也拥有的发现状态。"""
        for name in other.discovered_tool_names():
            self.mark_discovered(name)

    def find_deferred_by_names(self, names: list[str]) -> list[BaseTool]:
        """按主名称或别名精确查找延迟工具。"""
        matches: list[BaseTool] = []
        for name in names:
            tool = self.get(name)
            if tool is None:
                tool = self._get_case_insensitive(name)
            if tool is not None and self.is_deferred(tool):
                matches.append(tool)
        return _dedupe_tools(matches)

    def search_deferred(self, query: str, *, max_results: int = 5) -> list[ToolSearchMatch]:
        """按名称、别名、描述和 search_hint 检索延迟工具。"""
        terms = _query_terms(query)
        if not terms:
            return []
        matches: list[ToolSearchMatch] = []
        for tool in self.deferred_tools():
            score, reasons = _score_tool(tool, terms, query)
            if score > 0:
                matches.append(ToolSearchMatch(tool=tool, score=score, reasons=tuple(reasons)))
        matches.sort(key=lambda item: (-item.score, item.tool.name))
        return matches[:max_results]

    def _get_case_insensitive(self, name: str) -> BaseTool | None:
        """按大小写不敏感名称查找工具，服务 select: 容错。"""
        lowered = name.lower()
        for registered_name, tool in self._tools.items():
            if registered_name.lower() == lowered:
                return tool
        return None

    def schemas_for_model(self) -> list[dict[str, Any]]:
        """把当前已暴露的工具转换成模型 API 需要的 schema 列表。"""
        return [
            {
                "name": tool.name,
                "description": _schema_description(self, tool),
                "input_schema": tool.json_schema(),
            }
            for tool in self.list_tools()
            if not self.is_deferred(tool) or tool.name in self._discovered
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
    from .tool_search.Tool_Search import ToolSearchTool
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
        ToolSearchTool(),
        WebFetchTool(),
        WebSearchTool(),
        TaskCreateTool(),
        TaskUpdateTool(),
        TaskListTool(),
        TaskGetTool(),
        TaskClaimTool(),
        TaskBlockTool(),
        EnterPlanModeTool(),
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


def _query_terms(query: str) -> list[str]:
    return _dedupe([term.lower() for term in re.findall(r"[A-Za-z0-9_.:-]+", query) if term.strip()])


def _score_tool(tool: BaseTool, terms: list[str], raw_query: str) -> tuple[int, list[str]]:
    names = [tool.name, *getattr(tool, "aliases", ())]
    lower_names = [name.lower() for name in names]
    name_parts = _tool_name_parts(tool.name)
    description = (tool.description or "").lower()
    search_hint = (getattr(tool, "search_hint", "") or "").lower()
    is_mcp = bool(getattr(tool, "is_mcp", False))
    score = 0
    reasons: list[str] = []
    raw = raw_query.strip().lower()
    if raw and raw in lower_names:
        score += 14 if is_mcp else 12
        reasons.append("exact-name")
    for term in terms:
        if term in lower_names:
            score += 12 if is_mcp else 10
            reasons.append(f"name:{term}")
            continue
        if term in name_parts:
            score += 12 if is_mcp else 10
            reasons.append(f"name-part:{term}")
            continue
        if any(term in part for part in name_parts):
            score += 6 if is_mcp else 5
            reasons.append(f"partial-name-part:{term}")
        if any(term in name for name in lower_names):
            score += 3
            reasons.append(f"partial-name:{term}")
        if search_hint and term in search_hint:
            score += 4
            reasons.append(f"hint:{term}")
        if re.search(rf"\b{re.escape(term)}\b", description):
            score += 2
            reasons.append(f"description:{term}")
    return score, _dedupe(reasons)


def _tool_name_parts(name: str) -> list[str]:
    if name.startswith("mcp__"):
        value = name.removeprefix("mcp__").lower().replace("__", " ").replace("_", " ").replace("-", " ")
    else:
        value = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).replace("_", " ").replace("-", " ").lower()
    return [part for part in re.split(r"\s+", value) if part]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _dedupe_tools(tools: list[BaseTool]) -> list[BaseTool]:
    seen: set[int] = set()
    out: list[BaseTool] = []
    for tool in tools:
        if id(tool) in seen:
            continue
        seen.add(id(tool))
        out.append(tool)
    return out


def _schema_description(registry: ToolRegistry, tool: BaseTool) -> str:
    """Return a detailed model-facing description with stable operational metadata."""
    parts = [" ".join((tool.description or "").split())]
    aliases = ", ".join(getattr(tool, "aliases", ()) or ())
    metadata = [
        f"permission_category={tool.permission_category}",
        f"state_effect={tool.state_effect}",
    ]
    if aliases:
        metadata.append(f"aliases={aliases}")
    if registry.is_deferred(tool):
        metadata.append("deferred=true; load with Tool_Search before use")
    elif getattr(tool, "always_load", False):
        metadata.append("always_loaded=true")
    parts.append("Metadata: " + "; ".join(metadata) + ".")
    return "\n\n".join(part for part in parts if part)
