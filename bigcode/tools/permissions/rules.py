"""RuleEngine：规则解析、匹配与运行时管理。"""
from __future__ import annotations

import fnmatch
import re
from typing import Literal

from ..base import PermissionDecision
from .models import PermissionRule, PermissionTarget


# ---------------------------------------------------------------------------
# RuleEngine
# ---------------------------------------------------------------------------

class RuleEngine:
    """管理并评估权限规则。

    持有 allow/deny/ask 三组独立规则列表，提供工具级和内容级两种匹配方法。
    """

    def __init__(
        self,
        allow_rules: list[PermissionRule] | None = None,
        deny_rules: list[PermissionRule] | None = None,
        ask_rules: list[PermissionRule] | None = None,
    ) -> None:
        self.allow_rules: list[PermissionRule] = list(allow_rules or [])
        self.deny_rules: list[PermissionRule] = list(deny_rules or [])
        self.ask_rules: list[PermissionRule] = list(ask_rules or [])

    # -- Tool-level matching --

    def match_tool(self, target: PermissionTarget, behavior: str) -> PermissionRule | None:
        """Return the first matching tool-level (pattern=None) rule."""
        rules: list[PermissionRule] = getattr(self, f"{behavior}_rules")
        for rule in rules:
            if rule.pattern is not None:
                continue
            if rule.tool_name in {"*", target.tool_name}:
                return rule
        return None

    # -- Content-level matching --

    def match_content(self, target: PermissionTarget, behavior: str) -> PermissionRule | None:
        """Return the first matching content-level (pattern is set) rule."""
        rules: list[PermissionRule] = getattr(self, f"{behavior}_rules")
        haystack = _target_haystack(target)
        for rule in rules:
            if rule.pattern is None:
                continue
            if rule.tool_name not in {"*", target.tool_name}:
                continue
            if fnmatch.fnmatch(haystack, rule.pattern):
                return rule
        return None

    # -- Mutation --

    def add_rule(self, rule: PermissionRule, behavior: str) -> None:
        """Append a rule to the specified behavior list."""
        getattr(self, f"{behavior}_rules").append(rule)

    def remove_rule(self, rule: PermissionRule, behavior: str) -> None:
        """Remove a rule from the specified behavior list by equality."""
        rules: list[PermissionRule] = getattr(self, f"{behavior}_rules")
        rules[:] = [r for r in rules if r != rule]


# ---------------------------------------------------------------------------
# Standalone helpers used by the pipeline
# ---------------------------------------------------------------------------

def _target_haystack(target: PermissionTarget) -> str:
    """Build a single string from target fields for fnmatch matching."""
    return target.command or str(target.path or "") or target.network_url or ""


def _decision_from_rule(
    rule: PermissionRule,
    behavior: Literal["allow", "deny", "ask"],
    fallback: str,
) -> PermissionDecision:
    """Create a PermissionDecision carrying rule metadata."""
    return PermissionDecision(
        behavior,
        message=rule.reason or fallback,
        reason=rule.reason,
        rule=rule.source,
        decision_reason={
            "type": "rule",
            "rule": {
                "ruleBehavior": behavior,
                "source": rule.source,
                "toolName": rule.tool_name,
                "pattern": rule.pattern,
            },
        },
    )


def _is_unrelaxable(decision: PermissionDecision) -> bool:
    """deny 和特殊 ask 不能被 bypass 或整工具 allow 覆盖。"""
    if decision.behavior == "deny":
        return True
    return decision.behavior == "ask" and decision.reason_type in {
        "rule", "safetyCheck", "requiresUserInteraction",
    }


# ---------------------------------------------------------------------------
# Rule string parsing
# ---------------------------------------------------------------------------

def parse_permission_rule_string(
    raw: str,
    behavior: Literal["allow", "deny", "ask"],
    *,
    source: str = "config",
) -> PermissionRule:
    """解析配置字符串：Bash 是整工具规则，Bash(pattern) 是内容级规则。"""

    text = raw.strip()
    match = re.fullmatch(r"([^()]+)\((.*)\)", text)
    if match:
        return PermissionRule(
            tool_name=match.group(1).strip(),
            behavior=behavior,
            pattern=match.group(2),
            source=source,
        )
    return PermissionRule(tool_name=text, behavior=behavior, source=source)
