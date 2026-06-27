"""Slash command registry for the interactive REPL."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable


class CommandType(str, Enum):
    """Classify where a slash command is handled."""

    LOCAL = "local"
    LOCAL_UI = "local_ui"
    EXIT = "exit"


@dataclass
class CommandContext:
    """Runtime state passed to slash command handlers."""

    args: str
    session: Any
    ui: Any
    repl: Any | None = None


CommandHandler = Callable[[CommandContext], Awaitable[bool | None]]


@dataclass
class Command:
    """A registered slash command."""

    name: str
    description: str
    type: CommandType
    handler: CommandHandler
    aliases: list[str] = field(default_factory=list)
    usage: str = ""
    arg_prompt: str = ""
    hidden: bool = False


class CommandRegistry:
    """Register, look up, and list slash commands."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._alias_map: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def register(self, command: Command) -> None:
        async with self._lock:
            self.register_sync(command)

    def register_sync(self, command: Command) -> None:
        name = _normalize_name(command.name)
        aliases = [_normalize_name(alias) for alias in command.aliases]
        if name in self._commands or name in self._alias_map:
            raise ValueError(f"Command name {command.name!r} conflicts with an existing command or alias")
        for alias in aliases:
            if alias in self._commands or alias in self._alias_map:
                raise ValueError(f"Alias {alias!r} conflicts with an existing command or alias")
        command.name = name
        command.aliases = aliases
        self._commands[name] = command
        for alias in aliases:
            self._alias_map[alias] = name

    def find(self, name: str) -> Command | None:
        normalized = _normalize_name(name)
        if normalized in self._commands:
            return self._commands[normalized]
        canonical = self._alias_map.get(normalized)
        if canonical:
            return self._commands.get(canonical)
        return None

    def list_commands(self) -> list[Command]:
        return [command for command in self._commands.values() if not command.hidden]


def _normalize_name(name: str) -> str:
    return name.strip().lstrip("/").lower()
