"""Built-in slash commands."""
from __future__ import annotations

from bigcode.commands.registry import CommandRegistry

from .core import ALL_COMMANDS


def register_all_commands(registry: CommandRegistry) -> None:
    for command in ALL_COMMANDS:
        registry.register_sync(command)
