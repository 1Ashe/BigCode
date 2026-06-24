"""Sandbox configuration data models. Zero external dependencies beyond stdlib."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NetworkSandboxConfig:
    allowed_domains: list[str] = field(default_factory=list)
    denied_domains: list[str] = field(default_factory=list)
    allow_local_binding: bool = False
    allow_all_unix_sockets: bool = False


@dataclass
class FilesystemSandboxConfig:
    allow_write: list[str] = field(default_factory=list)
    deny_write: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=list)
    allow_read: list[str] = field(default_factory=list)


@dataclass
class SandboxConfig:
    enabled: bool = True
    auto_allow_bash_if_sandboxed: bool = False
    allow_unsandboxed_commands: bool = True
    fail_if_unavailable: bool = False
    enable_weaker_nested_sandbox: bool = False
    network: NetworkSandboxConfig = field(default_factory=NetworkSandboxConfig)
    filesystem: FilesystemSandboxConfig = field(default_factory=FilesystemSandboxConfig)
    excluded_commands: list[str] = field(default_factory=list)
    # Paths that should be scrubbed after each sandboxed command
    scrub_paths: list[str] = field(default_factory=list)


@dataclass
class SandboxDependencyCheck:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
