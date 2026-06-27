"""Interactive REPL orchestration for the terminal UI."""
from __future__ import annotations

import asyncio
import signal
import sys
import threading
from typing import Any

from bigcode.commands import CommandContext, CommandRegistry, parse_command
from bigcode.commands.handlers import register_all_commands

from .console import BigCodeTUI
from .prompt import BigCodePromptUI, read_yes_no_plain
from .renderer import BigCodeStreamRenderer


class BigCodeRepl:
    """Owns terminal-only interaction around an AgentSession."""

    def __init__(self, session: Any, *, ui: BigCodeTUI | None = None) -> None:
        self.session = session
        self.ui = ui or BigCodeTUI(enabled=True)
        self.command_registry = CommandRegistry()
        register_all_commands(self.command_registry)
        self.prompt_ui: BigCodePromptUI | None = None
        self._terminal_input_busy = threading.Event()
        self._renderer: BigCodeStreamRenderer | None = None

    async def run(self) -> None:
        """Run the interactive command loop."""
        restore_terminal = _capture_terminal_restore(force_sane=True)
        await self.session.start()
        self.ui.header(self.session.session_id, self.session.model_ref)
        if self.session.config.config_errors:
            self.ui.warning("Config warnings:")
            for err in self.session.config.config_errors:
                self.ui.print(f"  - {err}")
        if not self.session.model_ref:
            self.ui.warning("No model configured. Add .bigcode/models.json with default_model before asking model-backed questions.")
        self.session.approval_callback = self.approve_tool_action
        self.session.terminal_interaction_callback = self.run_terminal_interaction
        try:
            if not sys.stdin.isatty():
                await self._run_piped_stdin()
                return
            await self._run_tty_loop()
        finally:
            self.session.approval_callback = None
            self.session.terminal_interaction_callback = None
            shutdown = getattr(self.session, "shutdown", None)
            if shutdown is not None:
                await shutdown()
            restore_terminal()

    async def _run_piped_stdin(self) -> None:
        """Process each non-empty stdin line as a REPL input."""
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("/"):
                should_exit = await self.handle_command(line)
                if should_exit:
                    break
                continue
            try:
                await self.run_turn(line, allow_escape_cancel=False)
            except Exception as exc:
                self.ui.error(format_exception(exc))
                continue

    async def _run_tty_loop(self) -> None:
        """Run prompt_toolkit-backed interactive input."""
        restore_terminal = _capture_terminal_restore(force_sane=True)
        try:
            self.prompt_ui = BigCodePromptUI(history_path=self._repl_history_path(), command_registry=self.command_registry)
        except RuntimeError as exc:
            self.ui.error(str(exc))
            restore_terminal()
            return
        try:
            while True:
                try:
                    line = await self.prompt_ui.read_prompt()
                except (EOFError, KeyboardInterrupt):
                    self.ui.print()
                    break
                line = line.strip()
                if not line:
                    continue
                self.ui.print(f"You: {line}")
                if line.startswith("/"):
                    should_exit = await self.handle_command(line)
                    if should_exit:
                        break
                    continue
                try:
                    await self.run_turn(line, allow_escape_cancel=True)
                except Exception as exc:
                    self.ui.error(format_exception(exc))
                    continue
                self.ui.print()
        finally:
            restore_terminal()

    def _repl_history_path(self) -> Any:
        return self.session.config.project_state_dir / "repl_history"

    async def approve_tool_action(self, line: str) -> bool:
        """Approve a single tool action using the active terminal input UI."""
        if self._renderer:
            self._renderer._clear_status()
        return await self.run_terminal_interaction(lambda: read_yes_no_plain(f"{line} [y/N] "))

    async def run_terminal_interaction(self, callback: Any) -> Any:
        """Run a blocking terminal interaction while the escape watcher is paused."""
        self._terminal_input_busy.set()
        try:
            if sys.stdin.isatty() and sys.platform != "win32":
                await asyncio.sleep(0.12)
            return await asyncio.to_thread(callback)
        finally:
            self._terminal_input_busy.clear()

    async def run_turn(self, prompt: str, *, allow_escape_cancel: bool) -> None:
        """Consume and render one session turn."""
        renderer = BigCodeStreamRenderer(self.ui)
        self._renderer = renderer

        async def consume() -> None:
            async for event in self.session.run_turn_stream(prompt):
                renderer.handle(event)

        self.session.abort_event.clear()
        task = asyncio.create_task(consume())
        restore_terminal = _capture_terminal_restore()

        def _sigint_handler() -> None:
            renderer.close()
            self.session.abort_event.set()
            task.cancel()

        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, _sigint_handler)
        except NotImplementedError:
            pass

        watcher: asyncio.Task[None] | None = None
        if allow_escape_cancel and sys.stdin.isatty():
            watcher = asyncio.create_task(self._watch_escape_cancel(task))
        try:
            await task
        except asyncio.CancelledError:
            self.session.abort_event.set()
        finally:
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except (NotImplementedError, RuntimeError):
                pass
            if watcher and not watcher.done():
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            renderer.close()
            restore_terminal()
            self.session.abort_event.clear()
            self._renderer = None

    async def _watch_escape_cancel(self, task: asyncio.Task[Any]) -> None:
        """Cancel the running turn when Esc is pressed outside terminal prompts."""
        if sys.platform == "win32":
            return
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        cbreak_active = False

        def enter_cbreak() -> None:
            nonlocal cbreak_active
            if not cbreak_active:
                tty.setcbreak(fd)
                cbreak_active = True

        def restore_terminal() -> None:
            nonlocal cbreak_active
            if cbreak_active:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                cbreak_active = False

        try:
            while not task.done():
                if self._terminal_input_busy.is_set():
                    restore_terminal()
                    await asyncio.sleep(0.1)
                    continue
                enter_cbreak()
                ready, _, _ = await asyncio.to_thread(select.select, [sys.stdin], [], [], 0.1)
                if self._terminal_input_busy.is_set():
                    restore_terminal()
                    continue
                if ready and sys.stdin.read(1) == "\x1b":
                    self.session.abort_event.set()
                    task.cancel()
                    return
        finally:
            restore_terminal()

    async def handle_command(self, line: str) -> bool:
        """Handle local slash commands."""
        name, args, is_command = parse_command(line)
        if not is_command:
            return False
        if not name:
            await self.command_registry.find("help").handler(self._command_context(""))
            return False
        command = self.command_registry.find(name)
        if command is None:
            self.ui.print(f"Unknown command: /{name}")
            return False
        result = await command.handler(self._command_context(args))
        return bool(result)

    def _command_context(self, args: str) -> CommandContext:
        return CommandContext(args=args, session=self.session, ui=self.ui, repl=self)

    def status_rows(self) -> dict[str, Any]:
        """Build rows for /status."""
        return self._status_rows()

    def _status_rows(self) -> dict[str, Any]:
        rows: dict[str, Any] = {
            "session": self.session.session_id,
            "cwd": self.session.config.cwd,
            "model": self.session.model_ref,
            "protocol": self.session.model_protocol_label(),
            "permission mode": self.session.permission_context.mode,
            "messages": len(self.session.messages),
            "loaded skills": ", ".join(sorted(self.session.loaded_skills)) if self.session.loaded_skills else "(none)",
        }
        if self.session.last_verification:
            rows["last verification"] = f"{self.session.last_verification.get('command')} (exit {self.session.last_verification.get('exit_code')})"
        counts = self.session.agent_task_store.status_counts()
        total_background = sum(counts.values())
        rows["background subagents"] = (
            f"{total_background} "
            f"(queued {counts.get('queued', 0)}, running {counts.get('running', 0)}, "
            f"completed {counts.get('completed', 0)}, failed {counts.get('failed', 0)}, "
            f"cancelled {counts.get('cancelled', 0)})"
        )
        rows["fastmcp available"] = self.session.mcp_manager.fastmcp_available
        return rows


async def run_repl(session: Any) -> None:
    """Run a session in the terminal REPL UI."""
    await BigCodeRepl(session).run()


def format_exception(exc: Exception) -> str:
    """把异常转成非空字符串，给命令行显示使用。"""
    return str(exc).strip() or exc.__class__.__name__


def _capture_terminal_restore(*, force_sane: bool = False) -> Any:
    """Capture current terminal attrs and return an idempotent restore callback."""
    if sys.platform == "win32" or not sys.stdin.isatty():
        return lambda: None
    try:
        import termios

        fd = sys.stdin.fileno()
        settings = termios.tcgetattr(fd)
    except Exception:
        return lambda: None

    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        restored = True
        try:
            target = list(settings)
            if force_sane:
                target = _sane_terminal_settings(termios, target)
            termios.tcsetattr(fd, termios.TCSADRAIN, target)
        except Exception:
            pass

    return restore


def _sane_terminal_settings(termios: Any, settings: list[Any]) -> list[Any]:
    """Force essential interactive terminal flags back on."""
    sane = list(settings)
    sane[3] |= termios.ECHO | termios.ICANON | termios.ISIG
    if hasattr(termios, "IEXTEN"):
        sane[3] |= termios.IEXTEN
    if hasattr(termios, "ICRNL"):
        sane[0] |= termios.ICRNL
    if hasattr(termios, "OPOST"):
        sane[1] |= termios.OPOST
    return sane
