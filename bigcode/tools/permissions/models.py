"""权限系统的数据模型、类型别名和常量。零内部依赖。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

PermissionMode = Literal[
    "default", "acceptEdits", "plan", "bypassPermissions", "dontAsk"
]


@dataclass
class PermissionRule:
    """一条权限规则。

    pattern 为空表示整工具规则；pattern 非空表示内容级规则。
    """

    tool_name: str
    behavior: Literal["allow", "deny", "ask"]
    pattern: str | None = None
    source: str = "session"
    reason: str = ""


@dataclass
class ToolPermissionContext:
    """当前会话的权限模式和显式 allow/deny/ask 规则。"""

    mode: PermissionMode = "default"
    always_allow: list[PermissionRule] = field(default_factory=list)
    always_deny: list[PermissionRule] = field(default_factory=list)
    always_ask: list[PermissionRule] = field(default_factory=list)
    should_avoid_permission_prompts: bool = False


@dataclass(frozen=True)
class PermissionTarget:
    """从具体工具输入中抽象出的权限判断对象。"""

    tool_name: str
    category: str
    path: Path | None = None
    command: str | None = None
    network_url: str | None = None
    raw: BaseModel | None = None


# ---------------------------------------------------------------------------
# Safety / classification constants
# ---------------------------------------------------------------------------

SENSITIVE_NAMES: set[str] = {
    ".env",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
}

SYSTEM_DENY_PREFIXES: list[Path] = [
    Path(p) for p in ["/bin", "/sbin", "/usr", "/etc", "/var", "/boot", "/dev", "/proc", "/sys"]
]

READ_ONLY_BASH: set[str] = {
    "pwd", "ls", "find", "rg", "grep", "cat", "sed", "head", "tail", "wc", "git",
}

MUTATING_BASH: set[str] = {
    "rm", "mv", "cp", "chmod", "chown", "touch", "mkdir", "rmdir",
    "npm", "pip", "conda", "uv", "cargo", "go", "make", "docker", "kubectl", "git",
}

COMPLEX_SHELL_RE: re.Pattern = re.compile(r"(&&|\|\||;|\||>|<|\$\(|`|\n|\*)")
