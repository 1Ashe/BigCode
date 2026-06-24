"""权限决策系统。

公共 API 与旧版 flat permissions.py 保持向后兼容。
"""
from __future__ import annotations

from .helpers import allow_with_mode_policy
from .models import (
    COMPLEX_SHELL_RE,
    MUTATING_BASH,
    READ_ONLY_BASH,
    SENSITIVE_NAMES,
    SYSTEM_DENY_PREFIXES,
    PermissionMode,
    PermissionRule,
    PermissionTarget,
    ToolPermissionContext,
)
from .pipeline import (
    build_permission_target,
    check_content_policy,
    check_mode_policy_for_target,
    decide_permission,
)
from .rules import RuleEngine, parse_permission_rule_string
from .safety import DangerousCommandDetector, check_hard_deny, check_safety_for_target, classify_bash

__all__ = [
    "PermissionMode",
    "PermissionRule",
    "ToolPermissionContext",
    "PermissionTarget",
    "SENSITIVE_NAMES",
    "SYSTEM_DENY_PREFIXES",
    "READ_ONLY_BASH",
    "MUTATING_BASH",
    "COMPLEX_SHELL_RE",
    "decide_permission",
    "build_permission_target",
    "check_content_policy",
    "check_mode_policy_for_target",
    "check_safety_for_target",
    "check_hard_deny",
    "classify_bash",
    "parse_permission_rule_string",
    "allow_with_mode_policy",
    "DangerousCommandDetector",
    "RuleEngine",
]
