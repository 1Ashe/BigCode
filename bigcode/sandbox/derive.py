"""Derive sandbox configuration from permission rules (1:1 mapping)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from bigcode.tools.permissions.models import SYSTEM_DENY_PREFIXES

if TYPE_CHECKING:
    from bigcode.tools.permissions.models import ToolPermissionContext

from .models import FilesystemSandboxConfig, SandboxConfig

# ---------------------------------------------------------------------------
# Bare git repo detection
# ---------------------------------------------------------------------------

_BARE_GIT_REPO_FILES = ["HEAD", "objects", "refs", "hooks", "config"]


def _detect_bare_git_repo_files(cwd: Path) -> tuple[list[str], list[str]]:
    """Return (existing_paths_to_denywrite, non_existing_paths_to_scrub).

    Existing bare-repo files at cwd are denied writes (ro-bind).
    Non-existing ones are scrubbed post-command to remove any planted files.
    """
    deny: list[str] = []
    scrub: list[str] = []
    for name in _BARE_GIT_REPO_FILES:
        p = cwd / name
        try:
            if p.exists():
                deny.append(str(p))
            else:
                scrub.append(str(p))
        except OSError:
            pass
    return deny, scrub


# ---------------------------------------------------------------------------
# Hard-coded deny-write paths
# ---------------------------------------------------------------------------


def _collect_hard_deny_write_paths(
    config_roots: list[Path],
    bigcode_home: Path,
    cwd: Path,
    skill_roots: list[Path],
    agent_roots: list[Path],
) -> tuple[list[str], list[str]]:
    """Collect all hard-coded deny-write paths.

    Returns (deny_write, scrub_paths).
    """
    deny: list[str] = []

    # 1. Config files in every config root (settings, models, MCP)
    for root in config_roots:
        for name in ("settings.json", "settings.local.json", "models.json", "mcp.json"):
            deny.append(str(root / name))

    # 2. .bigcode/{skills,commands,agents} in every config root
    for root in config_roots:
        for sub in ("skills", "commands", "agents"):
            deny.append(str(root / sub))

    # 3. User-level ~/.bigcode/{skills,commands,agents}
    for sub in ("skills", "commands", "agents"):
        deny.append(str(bigcode_home / sub))

    # 4. Skill roots (additional paths where skills are loaded from)
    for skill_root in skill_roots:
        deny.append(str(skill_root))

    # 5. Agent roots
    for agent_root in agent_roots:
        deny.append(str(agent_root))

    # 6. System paths from hard-coded deny prefixes
    for prefix in SYSTEM_DENY_PREFIXES:
        deny.append(str(prefix))

    # 7. Bare git repo files
    bare_deny, bare_scrub = _detect_bare_git_repo_files(cwd)
    deny.extend(bare_deny)

    return deny, bare_scrub


# ---------------------------------------------------------------------------
# Main derivation function
# ---------------------------------------------------------------------------


def derive_sandbox_config(
    permission_context: ToolPermissionContext,
    sandbox_config: SandboxConfig | None,
    *,
    cwd: Path,
    workspace_roots: list[Path],
    config_roots: list[Path],
    skill_roots: list[Path],
    agent_roots: list[Path],
    bigcode_home: Path,
    temp_dir: str,
) -> SandboxConfig:
    """Derive the effective sandbox config from permission rules.

    If no user sandbox_config is provided, returns a disabled config.
    """
    if sandbox_config is None:
        return SandboxConfig(enabled=False)

    fs = FilesystemSandboxConfig(
        allow_write=list(sandbox_config.filesystem.allow_write),
        deny_write=list(sandbox_config.filesystem.deny_write),
        deny_read=list(sandbox_config.filesystem.deny_read),
        allow_read=list(sandbox_config.filesystem.allow_read),
    )

    # --- allowWrite: base paths ---
    fs.allow_write.append(str(cwd))
    fs.allow_write.append(temp_dir)
    for root in workspace_roots:
        fs.allow_write.append(str(root))

    # --- allowWrite: from Edit/Write allow rules ---
    for rule in permission_context.always_allow:
        if rule.tool_name in ("Edit", "Write") and rule.pattern:
            resolved = _resolve_sandbox_path(rule.pattern, cwd)
            if resolved:
                fs.allow_write.append(resolved)

    # --- denyWrite: hard-coded ---
    hard_deny, scrub_paths = _collect_hard_deny_write_paths(
        config_roots, bigcode_home, cwd, skill_roots, agent_roots
    )
    for path in hard_deny:
        if path not in fs.deny_write:
            fs.deny_write.append(path)

    # --- denyWrite: from Edit/Write deny rules ---
    for rule in permission_context.always_deny:
        if rule.tool_name in ("Edit", "Write") and rule.pattern:
            resolved = _resolve_sandbox_path(rule.pattern, cwd)
            if resolved:
                fs.deny_write.append(resolved)

    # --- denyRead: from Read deny rules ---
    for rule in permission_context.always_deny:
        if rule.tool_name == "Read" and rule.pattern:
            resolved = _resolve_sandbox_path(rule.pattern, cwd)
            if resolved:
                fs.deny_read.append(resolved)

    # --- Network: from WebFetch rules ---
    network = sandbox_config.network
    allowed_domains = list(network.allowed_domains)
    denied_domains = list(network.denied_domains)
    for rule in permission_context.always_allow:
        if rule.tool_name == "WebFetch" and rule.pattern and rule.pattern.startswith("domain:"):
            domain = rule.pattern[len("domain:"):]
            if domain and domain not in allowed_domains:
                allowed_domains.append(domain)
    for rule in permission_context.always_deny:
        if rule.tool_name == "WebFetch" and rule.pattern and rule.pattern.startswith("domain:"):
            domain = rule.pattern[len("domain:"):]
            if domain and domain not in denied_domains:
                denied_domains.append(domain)

    from .models import NetworkSandboxConfig
    network = NetworkSandboxConfig(
        allowed_domains=allowed_domains,
        denied_domains=denied_domains,
        allow_local_binding=network.allow_local_binding,
        allow_all_unix_sockets=network.allow_all_unix_sockets,
    )

    return SandboxConfig(
        enabled=sandbox_config.enabled,
        auto_allow_bash_if_sandboxed=sandbox_config.auto_allow_bash_if_sandboxed,
        allow_unsandboxed_commands=sandbox_config.allow_unsandboxed_commands,
        fail_if_unavailable=sandbox_config.fail_if_unavailable,
        enable_weaker_nested_sandbox=sandbox_config.enable_weaker_nested_sandbox,
        network=network,
        filesystem=fs,
        excluded_commands=list(sandbox_config.excluded_commands),
        scrub_paths=scrub_paths,
    )


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _resolve_sandbox_path(pattern: str, cwd: Path) -> str | None:
    """Resolve a permission-rule path pattern to an absolute sandbox path.

    CC conventions:
      //path  → absolute from filesystem root (/path)
      /path   → relative to cwd (since settings root ≈ cwd for our config roots)
      ~/path  → expand home directory
      path    → relative to cwd
    """
    if pattern.startswith("//"):
        return pattern[1:]
    if pattern.startswith("~"):
        return str(Path(pattern).expanduser())
    if pattern.startswith("/"):
        return str((cwd / pattern.lstrip("/")).resolve())
    # Relative path, or pattern with wildcards — use cwd
    if any(c in pattern for c in "*?[]"):
        # For wildcard patterns, resolve the longest non-wildcard prefix
        return _resolve_glob_prefix(pattern, cwd)
    return str((cwd / pattern).resolve())


def _resolve_glob_prefix(pattern: str, cwd: Path) -> str | None:
    """For glob patterns like 'src/**', resolve 'src' as a relative path."""
    # Strip trailing /** for directory-level mount
    clean = pattern.rstrip("/")
    if clean.endswith("/**"):
        clean = clean[:-3]
    # Find the first glob character
    first_glob = min(
        (i for i, c in enumerate(clean) if c in "*?[]"),
        default=len(clean),
    )
    prefix = clean[:first_glob].rstrip("/")
    if not prefix:
        return str(cwd)
    resolved = (cwd / prefix).resolve()
    if resolved.exists():
        return str(resolved)
    return str(cwd)
