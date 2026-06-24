"""内置 subAgent 类型定义。

学习思路：每个 AgentDefinition 描述子代理的系统提示词、允许工具、权限模式和最多轮数。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentDefinition:
    """一种内置子代理的配置。

    定义子代理提示词、允许工具、权限模式、模型覆盖和最多轮数。
    """
    name: str
    description: str
    system_prompt: str
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    model: str | None = None
    permission_mode: str | None = None
    max_turns: int = 50
    background: bool = False


def get_builtin_agents() -> list[AgentDefinition]:
    """返回 BigCode 内置的子代理定义列表。"""
    return [
        # general-purpose 不限制工具，适合明确授权后的实现或综合调查任务。
        AgentDefinition(
            name="general-purpose",
            description="General coding subAgent for bounded implementation or investigation tasks.",
            system_prompt="You are a focused coding subAgent. Complete the delegated task and return concise results.",
            tools=None,
            max_turns=50,
        ),
        # explorer 是只读代码探索者，permission_mode=plan 会禁止写入。
        AgentDefinition(
            name="explorer",
            description="Read-only code explorer for locating files, APIs, tests, and likely causes.",
            system_prompt="You are a read-only explorer. Inspect code and report findings with file paths. Do not edit.",
            tools=["Read", "Glob", "Grep", "Bash", "SkillLoad", "SkillResourceRead", "ExternalResourceRead", "ExternalPromptGet"],
            permission_mode="plan",
            max_turns=50,
        ),
        # code-reviewer 也是只读，但提示词偏向发现缺陷和测试缺口。
        AgentDefinition(
            name="code-reviewer",
            description="Read-only reviewer for bugs, regressions, and missing tests.",
            system_prompt="Review code changes for defects. Report findings first, with file references.",
            tools=["Read", "Glob", "Grep", "Bash"],
            permission_mode="plan",
            max_turns=50,
        ),
        # planAgent 用来把已有发现整理成计划，工具和权限都保持只读。
        AgentDefinition(
            name="planAgent",
            description="Read-only planning assistant that turns findings into an implementation plan.",
            system_prompt="Create a decision-complete implementation plan from provided context. Do not edit files.",
            tools=["Read", "Glob", "Grep", "Bash"],
            permission_mode="plan",
            max_turns=50,
        ),
    ]


def builtin_agent_map() -> dict[str, AgentDefinition]:
    """把内置子代理列表转换成 name -> AgentDefinition 的查找表。"""
    return {agent.name: agent for agent in get_builtin_agents()}
