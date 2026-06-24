"""Bubblewrap command builder and dependency detection."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import SandboxConfig, SandboxDependencyCheck


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def detect_platform() -> str:
    """Return "linux" | "wsl" | "unsupported"."""
    if os.name != "posix":
        return "unsupported"
    # Check for WSL via /proc/version
    try:
        version = Path("/proc/version").read_text()
        if "microsoft" in version.lower() or "wsl" in version.lower():
            # WSL2 reports kernel 4.19+, WSL1 reports 4.4.x
            # If major >= 5 it's definitely WSL2
            try:
                kernel = version.strip().split()[2]  # e.g. "6.18.33.1-microsoft-standard-WSL2"
                major_str = kernel.split(".")[0]
                major = int(major_str) if major_str.isdigit() else 0
                if major >= 5:
                    return "wsl"
                # major == 4: check minor >= 19
                minor_str = kernel.split(".")[1] if "." in kernel else "0"
                minor = int(minor_str) if minor_str.isdigit() else 0
                if major == 4 and minor >= 19:
                    return "wsl"
                return "unsupported"  # WSL1
            except Exception:
                return "wsl"  # Can't parse, assume WSL2
        return "linux"
    except OSError:
        return "linux"


# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------


def check_dependencies() -> SandboxDependencyCheck:
    """Check whether bwrap and optional socat are available."""
    from .models import SandboxDependencyCheck

    result = SandboxDependencyCheck()

    if not shutil.which("bwrap"):
        result.errors.append(
            "bwrap not found. Install bubblewrap: sudo apt install bubblewrap"
        )

    if not shutil.which("socat"):
        result.warnings.append(
            "socat not found; network domain filtering will use full network isolation "
            "instead of proxy-based filtering. Install socat for domain-level filtering."
        )

    return result


# ---------------------------------------------------------------------------
# should_use_sandbox
# ---------------------------------------------------------------------------


def should_use_sandbox(
    *,
    command: str,
    dangerously_disable_sandbox: bool,
    config: SandboxConfig | None,
) -> bool:
    """Decide whether to wrap a Bash command with bwrap."""
    if config is None or not config.enabled:
        return False

    platform = detect_platform()
    if platform not in ("linux", "wsl"):
        return False

    if dangerously_disable_sandbox and config.allow_unsandboxed_commands:
        return False

    # Check excluded commands
    if config.excluded_commands:
        stripped = command.strip()
        for excluded in config.excluded_commands:
            if _command_matches_excluded(stripped, excluded):
                return False

    return True


def _command_matches_excluded(command: str, pattern: str) -> bool:
    """Check if a command matches an excluded command pattern.

    Patterns can be:
      - Exact match: "docker" matches only "docker"
      - Prefix match: "docker " (with trailing space) matches "docker ps", "docker build"
      - Wildcard: "npm *" matches "npm test", "npm install"
    """
    if pattern.endswith(" *"):
        prefix = pattern[:-2]
        return command == prefix or command.startswith(prefix + " ")
    if " " not in pattern:
        # Exact match or prefix: "docker" matches "docker" and "docker ps"
        parts = command.split()
        if not parts:
            return False
        if parts[0] == pattern:
            return True
        return False
    # Pattern contains spaces: exact prefix match
    return command == pattern or command.startswith(pattern + " ")


# ---------------------------------------------------------------------------
# Bubblewrap command builder
# ---------------------------------------------------------------------------


class BubblewrapBuilder:
    """Build a bwrap argument list from SandboxConfig."""

    def __init__(self, config: SandboxConfig):
        self._config = config
        self._fs = config.filesystem

    def build(
        self, command: str, *, shell_path: str = "/bin/bash", chdir: str, tmp_dir: str
    ) -> list[str]:
        """Return the bwrap argument list for asyncio.create_subprocess_exec."""
        args = ["bwrap"]

        # Namespace isolation
        args += ["--unshare-all"]
        args += ["--share-net"]  # Will be replaced if network is restricted

        # Read-only system directories
        for sys_dir in ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"]:
            if os.path.exists(sys_dir):
                args += ["--ro-bind", sys_dir, sys_dir]

        # Virtual filesystems
        args += ["--proc", "/proc"]
        args += ["--dev", "/dev"]
        args += ["--tmpfs", "/tmp"]

        # Sandbox temp directory
        args += ["--bind", tmp_dir, tmp_dir]

        # Working directory
        args += ["--bind", chdir, chdir]
        args += ["--chdir", chdir]

        # allowWrite paths (sorted by depth: parents before children)
        seen_write: set[str] = set()
        for path in sorted(set(self._fs.allow_write), key=lambda p: (p.count("/"), p)):
            if path in seen_write:
                continue
            seen_write.add(path)
            if os.path.exists(path):
                args += ["--bind", path, path]

        # denyWrite (ro-bind overrides parent rw mounts)
        for path in self._fs.deny_write:
            if os.path.exists(path):
                args += ["--ro-bind", path, path]
            else:
                args += ["--ro-bind", "/dev/null", path]

        # denyRead
        for path in self._fs.deny_read:
            args += ["--ro-bind", "/dev/null", path]

        # allowRead (override denyRead)
        for path in self._fs.allow_read:
            if os.path.exists(path):
                args += ["--ro-bind", path, path]

        # Network isolation
        network = self._config.network
        if network.allowed_domains or network.denied_domains:
            args = [a for a in args if a != "--share-net"]
            args += ["--unshare-net"]

        # Environment
        args += ["--clearenv"]
        args += ["--setenv", "PATH", "/usr/bin:/bin:/usr/local/bin"]
        home = os.environ.get("HOME", "/root")
        args += ["--setenv", "HOME", home]
        args += ["--setenv", "TMPDIR", tmp_dir]
        args += ["--setenv", "TMP", tmp_dir]

        # Safety
        args += ["--die-with-parent"]
        args += ["--new-session"]

        # Separator + command
        args += ["--", shell_path, "-c", command]
        return args


# ---------------------------------------------------------------------------
# Post-command cleanup
# ---------------------------------------------------------------------------


def scrub_after_command(scrub_paths: list[str]) -> None:
    """Remove any files planted at scrub_paths during a sandboxed command."""
    for path in scrub_paths:
        try:
            p = Path(path)
            if p.is_dir():
                shutil.rmtree(path)
            elif p.exists():
                p.unlink()
        except OSError:
            pass
