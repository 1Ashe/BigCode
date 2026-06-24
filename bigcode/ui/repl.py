"""Interactive REPL orchestration for the terminal UI."""
from __future__ import annotations

import asyncio
import signal
import sys
import threading
from typing import Any

from bigcode.context.compact import CompactDeps, apply_context_compact
from bigcode.diagnostics import build_doctor_report, render_doctor_report
from bigcode.tools.base import EmptyInput
from bigcode.tools.plan.EnterPlanMode import EnterPlanModeTool

from .console import BigCodeTUI
from .prompt import BigCodePromptUI, read_yes_no_plain
from .renderer import BigCodeStreamRenderer


SLASH_COMMANDS = ["/help", "/exit", "/quit", "/status", "/doctor", "/plan", "/compact"]


class BigCodeRepl:
    """Owns terminal-only interaction around an AgentSession."""

    def __init__(self, session: Any, *, ui: BigCodeTUI | None = None) -> None:
        self.session = session
        self.ui = ui or BigCodeTUI(enabled=True)
        self.prompt_ui: BigCodePromptUI | None = None
        self._terminal_input_busy = threading.Event()
        self._renderer: BigCodeStreamRenderer | None = None

    async def run(self) -> None:
        """Run the interactive command loop."""
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
        try:
            self.prompt_ui = BigCodePromptUI(history_path=self._repl_history_path(), slash_commands=SLASH_COMMANDS)
        except RuntimeError as exc:
            self.ui.error(str(exc))
            return
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
        cmd, _, arg = line.partition(" ")
        if cmd in {"/exit", "/quit"}:
            return True
        if cmd == "/help":
            self.ui.print("Commands: " + ", ".join(SLASH_COMMANDS))
            return False
        if cmd == "/doctor":
            parts = arg.split()
            report = await build_doctor_report(
                self.session.config,
                model_ref=self.session.model_ref,
                probe="--no-probe" not in parts,
                timeout=parse_timeout(parts),
                registry=self.session.registry,
                skill_registry=self.session.skill_registry,
                mcp_manager=self.session.mcp_manager,
            )
            self.ui.print(render_doctor_report(report), end="")
            return False
        if cmd == "/status":
            self.ui.status_table(self._status_rows())
            return False
        if cmd == "/plan":
            if not self.session.plan_state.active:
                await EnterPlanModeTool().call(EmptyInput(), self.session.make_tool_context())
                self.ui.print(f"Entered Plan Mode: {self.session.plan_state.plan_file}")
            else:
                content = self.session.plan_store.read(self.session.session_id) or ""
                self.ui.print(f"Plan file: {self.session.plan_state.plan_file}")
                self.ui.print(content or "(empty)")
            return False
        if cmd == "/compact":
            compacted = await apply_context_compact(
                self.session.messages,
                CompactDeps(
                    config=self.session.config.compact,
                    state=self.session.compact_state,
                    context_window=self.session.model.context_window or 128000,
                    system_prompt=self.session.system_prompt,
                    tool_schemas=self.session.registry.schemas_for_model(),
                    is_main_thread=self.session.is_main_thread,
                    summarize=self.session._summarize_context,
                ),
                force_auto=True,
            )
            self.session._append_compact_records(compacted.records_to_append)
            self.session.read_file_state.clear()
            self.session._save_snapshot()
            self.ui.print(
                "Compacted context: "
                f"{compacted.tokens_before} -> {compacted.tokens_after} tokens "
                f"({compacted.utilization_after:.1%})"
            )
            return False
        self.ui.print(f"Unknown command: {cmd}")
        return False

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


def parse_timeout(parts: list[str]) -> float:
    """从 /doctor 参数列表里读取 --timeout，失败时回退到默认 10 秒。"""
    if "--timeout" not in parts:
        return 10.0
    idx = parts.index("--timeout")
    try:
        return float(parts[idx + 1])
    except (IndexError, ValueError):
        return 10.0


def _capture_terminal_restore() -> Any:
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
            termios.tcsetattr(fd, termios.TCSADRAIN, settings)
        except Exception:
            pass

    return restore
