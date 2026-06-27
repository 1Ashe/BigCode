"""Slash command parsing and completion helpers."""
from __future__ import annotations

from bigcode.commands.registry import CommandRegistry


def parse_command(text: str) -> tuple[str, str, bool]:
    """Parse a REPL input line into command name and args."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return "", "", False
    body = stripped[1:].strip()
    if not body:
        return "", "", True
    parts = body.split(None, 1)
    name = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return name, args, True


def complete(registry: CommandRegistry, prefix: str) -> list[tuple[str, str]]:
    """Return completion rows as (display_text, command_value)."""
    normalized = prefix.strip().lstrip("/").lower()
    matches: list[tuple[str, str]] = []
    seen: set[str] = set()
    for command in registry.list_commands():
        if command.name in seen:
            continue
        if command.name.startswith(normalized) or any(alias.startswith(normalized) for alias in command.aliases):
            seen.add(command.name)
            desc = command.description.replace("[", "\\[")
            if len(desc) > 38:
                desc = desc[:36] + "..."
            matches.append((f"/{command.name:<16} - {desc}", f"/{command.name}"))
    matches.sort(key=lambda item: item[1])
    return matches[:8]
