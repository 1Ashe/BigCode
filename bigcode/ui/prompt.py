"""Prompt-toolkit input helpers for the terminal UI."""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from typing import Any

from bigcode.commands import CommandRegistry, complete

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.patch_stdout import patch_stdout
except ImportError:
    PromptSession = None  # type: ignore[assignment]
    Completer = object  # type: ignore[assignment]
    Completion = None  # type: ignore[assignment]
    FileHistory = None  # type: ignore[assignment]
    patch_stdout = None  # type: ignore[assignment]


INVALID_APPROVAL_PROMPT = "Please type yes/y to allow, no/n or Enter to deny: "


class SlashCommandCompleter(Completer):  # type: ignore[misc]
    """只在输入 / 命令时给出补全候选。"""

    def __init__(self, *, command_registry: CommandRegistry | None = None, commands: list[str] | None = None) -> None:
        self.command_registry = command_registry
        self.commands = commands or []

    def get_completions(self, document: Any, complete_event: Any) -> Any:
        if Completion is None:
            return
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        prefix = text.split(maxsplit=1)[0]
        if self.command_registry is not None:
            for display, value in complete(self.command_registry, prefix):
                yield Completion(value, start_position=-len(prefix), display=display)
            return
        for command in self.commands:
            if command.startswith(prefix):
                yield Completion(command, start_position=-len(prefix), display=command)


class BigCodePromptUI:
    """prompt_toolkit 驱动的交互式输入和单行审批 UI。"""

    def __init__(
        self,
        *,
        history_path: Path,
        command_registry: CommandRegistry | None = None,
        slash_commands: list[str] | None = None,
    ) -> None:
        if PromptSession is None or FileHistory is None or patch_stdout is None:
            raise RuntimeError("Interactive prompt UI requires prompt_toolkit. Install prompt_toolkit to use the BigCode REPL UI.")
        history_path.parent.mkdir(parents=True, exist_ok=True)
        self.command_registry = command_registry
        self.slash_commands = slash_commands or []
        self.session = PromptSession(
            history=FileHistory(str(history_path)),
            completer=SlashCommandCompleter(command_registry=command_registry, commands=self.slash_commands),
            complete_while_typing=True,
            reserve_space_for_menu=6,
        )
        self._prompt_async_supports_erase = "erase_when_done" in inspect.signature(self.session.prompt_async).parameters

    async def read_prompt(self) -> str:
        """读取一条用户输入；提交后清理输入框。"""
        return await self._prompt([("class:prompt", "BigCode"), ("", " > ")], erase=True)

    async def _prompt(self, message: Any, *, erase: bool) -> str:
        """调用 prompt_toolkit；旧版本不支持 erase_when_done 时自动降级。"""
        kwargs = {"erase_when_done": True} if erase and self._prompt_async_supports_erase else {}
        with patch_stdout():
            try:
                value = await self.session.prompt_async(message, **kwargs)
            except TypeError as exc:
                if "erase_when_done" not in str(exc):
                    raise
                self._prompt_async_supports_erase = False
                value = await self.session.prompt_async(message)
        if erase and not self._prompt_async_supports_erase:
            erase_previous_prompt_line()
        return value


def parse_yes_no(value: str) -> bool | None:
    """Parse approval input, tolerating quick repeated keypresses."""
    normalized = value.strip().lower()
    if normalized in {"", "n", "no"} or (normalized and set(normalized) == {"n"}):
        return False
    if normalized in {"y", "yes"} or (normalized and set(normalized) == {"y"}):
        return True
    return None


def read_yes_no_plain(prompt: str) -> bool:
    """普通终端 y/n fallback，用于 prompt_toolkit 不可用的审批路径。"""
    while True:
        try:
            value = input(prompt)
        except EOFError:
            return False
        parsed = parse_yes_no(value)
        if parsed is not None:
            return parsed
        prompt = INVALID_APPROVAL_PROMPT


async def approve_with_callback_or_stdin(line: str, approve_callback: Any | None) -> bool:
    """Ask for approval via UI callback when available, otherwise fallback to stdin."""
    if approve_callback is not None:
        return bool(await approve_callback(line))
    return await asyncio.to_thread(read_yes_no_plain, f"{line} [y/N] ")


def erase_previous_prompt_line(lines: int = 1) -> None:
    """Best-effort cleanup for prompt_toolkit versions without erase_when_done."""
    if not sys.stdout.isatty():
        return
    for _ in range(max(1, lines)):
        sys.stdout.write("\x1b[1A\r\x1b[2K")
    sys.stdout.flush()
