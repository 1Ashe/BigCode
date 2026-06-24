"""安全检查、Bash 命令分类与可扩展的危险命令检测器。"""
from __future__ import annotations

import ipaddress
import re
import shlex
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from ..base import PermissionDecision, ToolExecutionContext
from .models import (
    COMPLEX_SHELL_RE,
    MUTATING_BASH,
    READ_ONLY_BASH,
    SENSITIVE_NAMES,
    SYSTEM_DENY_PREFIXES,
    PermissionTarget,
)

# ---------------------------------------------------------------------------
# Default dangerous command patterns
# ---------------------------------------------------------------------------

_DANGEROUS_COMMAND_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(^|\s)(sudo|su)(\s|$)"), "Privilege escalation commands are denied."),
    (re.compile(r"rm\s+-[^\n]*r[^\n]*f\s+(/|~|\$HOME)(\s|$)"), "Broad recursive deletion is denied."),
]


class DangerousCommandDetector:
    """可扩展的 shell 危险命令检测器。

    内置 sudo/su 和 rm -rf 默认规则，可通过构造器注入或 add_pattern() 扩展。
    """

    def __init__(self, extra_patterns: list[tuple[re.Pattern, str]] | None = None) -> None:
        self._patterns: list[tuple[re.Pattern, str]] = list(_DANGEROUS_COMMAND_PATTERNS)
        if extra_patterns:
            self._patterns.extend(extra_patterns)

    def detect(self, command: str) -> tuple[bool, str | None]:
        """Return (is_dangerous, reason) for the given command string."""
        stripped = command.strip()
        for pattern, reason in self._patterns:
            if pattern.search(stripped):
                return True, reason
        return False, None

    def add_pattern(self, regex: re.Pattern, reason: str) -> None:
        """Append a new detection pattern at runtime."""
        self._patterns.append((regex, reason))


# Module-level default detector instance
_default_detector = DangerousCommandDetector()


# ---------------------------------------------------------------------------
# Hard deny (non-configurable safety checks)
# ---------------------------------------------------------------------------

def check_hard_deny(target: PermissionTarget, ctx: ToolExecutionContext) -> str | None:
    """执行不能被配置绕过的安全拒绝。"""

    if target.path is not None:
        name = target.path.name
        if name in SENSITIVE_NAMES or name.endswith(".pem"):
            return f"Access to sensitive file {name!r} is denied."
        try:
            resolved = _resolve_existing_or_parent(ctx.cwd, target.path)
        except Exception as exc:
            return f"Path could not be resolved safely: {exc}"
        if target.category in {"write", "edit", "delete"}:
            if _is_under_bigcode_dir(resolved):
                return "Writing to .bigcode directory is denied."
            for prefix in SYSTEM_DENY_PREFIXES:
                if _is_relative_to(resolved, prefix):
                    return f"Writes to system path {prefix} are denied."

    if target.command:
        hit, reason = _default_detector.detect(target.command)
        if hit:
            return reason

    if target.network_url:
        parsed = urlparse(target.network_url)
        if parsed.scheme not in {"http", "https"}:
            return "Only http and https URLs are allowed."
        if not parsed.hostname:
            return "URL must include a host."
        if _is_unsafe_network_host(parsed.hostname):
            return "Localhost, metadata, and private network targets are denied."

    return None


# ---------------------------------------------------------------------------
# Safety check (hard deny + bash classification)
# ---------------------------------------------------------------------------

def check_safety_for_target(
    target: PermissionTarget, ctx: ToolExecutionContext
) -> PermissionDecision | None:
    """执行不能被配置或 bypass 绕过的输入级安全检查。"""

    message = check_hard_deny(target, ctx)
    if message:
        return PermissionDecision(
            "deny", message=message, reason=message,
            decision_reason={"type": "safetyCheck"},
        )
    if target.tool_name == "Bash" and target.command:
        kind = classify_bash(target.command)
        if kind == "danger":
            return PermissionDecision(
                "deny", message="Dangerous shell command denied.",
                reason="bash-danger", decision_reason={"type": "safetyCheck"},
            )
        if kind == "unknown":
            return PermissionDecision(
                "ask", message="Shell command is not provably read-only.",
                reason="bash-unknown", decision_reason={"type": "safetyCheck"},
            )
    return None


# ---------------------------------------------------------------------------
# Bash command classification
# ---------------------------------------------------------------------------

def classify_bash(command: str) -> Literal["read", "mutate", "danger", "unknown"]:
    """把 shell 命令粗分为 read/mutate/danger/unknown。"""

    command = command.strip()
    if not command:
        return "read"
    if re.search(r"(^|\s)(sudo|su)(\s|$)", command):
        return "danger"
    if COMPLEX_SHELL_RE.search(command):
        return "unknown"
    try:
        parts = shlex.split(command)
    except ValueError:
        return "unknown"
    if not parts:
        return "read"
    exe = Path(parts[0]).name
    if exe == "git" and len(parts) > 1:
        sub = parts[1]
        if sub == "branch":
            if any(
                part in {"-d", "-D", "--delete", "--move", "-m", "-M", "--copy", "-c", "-C"}
                for part in parts[2:]
            ):
                return "mutate"
            read_flags = {
                "-a", "-r", "-v", "-vv", "--all", "--remotes", "--verbose",
                "--list", "--show-current", "--contains", "--merged", "--no-merged",
            }
            return (
                "read"
                if all(part.startswith("-") and part in read_flags for part in parts[2:])
                else ("read" if len(parts) == 2 else "mutate")
            )
        if sub in {"status", "diff", "show", "log", "rev-parse", "ls-files", "grep", "blame"}:
            return "read"
        return "mutate"
    if exe == "find":
        if any(part in {"-delete", "-exec", "-execdir", "-ok", "-okdir"} for part in parts[1:]):
            return "mutate"
        return "read"
    if exe == "sed":
        if any(part == "-i" or part.startswith("-i") for part in parts[1:]):
            return "mutate"
        return "read"
    if exe in {"python", "python3", "node", "ruby", "perl", "bash", "sh"}:
        if len(parts) == 2 and parts[1] in {"--version", "-V", "-v"}:
            return "read"
        return "unknown"
    if exe in READ_ONLY_BASH:
        return "read"
    if exe in MUTATING_BASH:
        return "mutate"
    return "unknown"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _resolve_existing_or_parent(cwd: Path, path: Path) -> Path:
    """解析已存在路径；新文件则解析其父目录再拼回文件名。"""

    candidate = path if path.is_absolute() else cwd / path
    if candidate.exists():
        return candidate.resolve(strict=True)
    return candidate.parent.resolve(strict=True) / candidate.name


def _inside_any(path: Path, roots: list[Path]) -> bool:
    """判断路径是否在任一允许根目录内部。"""
    return any(_is_relative_to(path, root) for root in roots)


def _is_relative_to(path: Path, root: Path) -> bool:
    """Path.relative_to 的安全包装，用于兼容不存在的路径。"""
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _is_under_bigcode_dir(path: Path) -> bool:
    """Return True if the path is inside a .bigcode directory."""
    return ".bigcode" in path.parts


def _is_unsafe_network_host(hostname: str) -> bool:
    """判断 URL 主机是否是 localhost、私网、链路本地或保留地址。"""

    host = hostname.strip("[]").lower()
    if host in {"localhost", "localhost.localdomain", "metadata.google.internal"} or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        ip.is_loopback or ip.is_link_local or ip.is_private
        or ip.is_unspecified or ip.is_multicast or ip.is_reserved
    )
