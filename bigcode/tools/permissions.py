"""向后兼容 re-export 模块。委托到 bigcode.tools.permissions 包。

所有从 bigcode.tools.permissions 导入的外部调用方继续工作，无需修改 import 路径。
"""
from __future__ import annotations

from bigcode.tools.permissions import (  # noqa: F401
    # Data models
    PermissionMode,
    PermissionRule,
    ToolPermissionContext,
    PermissionTarget,
    SENSITIVE_NAMES,
    SYSTEM_DENY_PREFIXES,
    READ_ONLY_BASH,
    MUTATING_BASH,
    COMPLEX_SHELL_RE,
    # Safety
    check_hard_deny,
    check_safety_for_target,
    classify_bash,
    DangerousCommandDetector,
    # Rules
    RuleEngine,
    parse_permission_rule_string,
    # Pipeline
    decide_permission,
    build_permission_target,
    check_content_policy,
    check_mode_policy_for_target,
    # Helpers
    allow_with_mode_policy,
)
