"""OS-level sandbox for Bash commands using bubblewrap (Linux/WSL)."""

from .bwrap import BubblewrapBuilder, check_dependencies, detect_platform, scrub_after_command, should_use_sandbox
from .derive import derive_sandbox_config
from .models import FilesystemSandboxConfig, NetworkSandboxConfig, SandboxConfig, SandboxDependencyCheck

__all__ = [
    "BubblewrapBuilder",
    "FilesystemSandboxConfig",
    "NetworkSandboxConfig",
    "SandboxConfig",
    "SandboxDependencyCheck",
    "check_dependencies",
    "derive_sandbox_config",
    "detect_platform",
    "scrub_after_command",
    "should_use_sandbox",
]
