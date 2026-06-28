from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from bigcode.agent.events import PermissionRequested, PermissionResolved, StatusEvent, StreamEvent, ToolCompleted, ToolStarted, TurnCompleted
from bigcode.plan import PlanModeState, PlanStore
from bigcode.tools.base import ToolExecutionContext
from bigcode.tools.permissions import ToolPermissionContext
from bigcode.tools.plan.AskUserQuestion import AskUserQuestionInput, AskUserQuestionTool, UserQuestion
from bigcode.tools.plan.ExitPlanMode import ExitPlanModeInput, ExitPlanModeTool
from bigcode.tools.read_file_state import ReadFileState
from bigcode.ui.repl import BigCodeRepl, _sane_terminal_settings
from bigcode.ui.renderer import BigCodeStreamRenderer


class FakeUI:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, str | None]] = []
        self.console = object()

    def print(self, *objects: object, end: str = "\n") -> None:
        self.calls.append(("print", objects, end))

    def stream_text(self, text: str) -> None:
        self.calls.append(("stream", text, None))

    def stream_marker(self, *, marker_style: str) -> None:
        self.calls.append(("marker", None, marker_style))

    def divider(self) -> None:
        self.calls.append(("divider", None, None))


class FakeStatus:
    instances: list["FakeStatus"] = []

    def __init__(self, renderable: object, *, console: object, spinner: str) -> None:
        self.messages = [str(renderable)]
        self.started = False
        self.stopped = False
        FakeStatus.instances.append(self)

    def start(self) -> None:
        self.started = True

    def update(self, renderable: object) -> None:
        self.messages.append(str(renderable))

    def stop(self) -> None:
        self.stopped = True


class FakeText:
    def __init__(self, message: str, *, style: str = "") -> None:
        self.message = message

    def __str__(self) -> str:
        return self.message


class FakePromptUI:
    lines: list[object] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._lines = list(FakePromptUI.lines)

    async def read_prompt(self) -> str:
        if not self._lines:
            raise EOFError
        value = self._lines.pop(0)
        if isinstance(value, BaseException):
            raise value
        return str(value)


class UIInteractionTests(unittest.TestCase):
    def test_renderer_keeps_tool_events_after_streamed_text(self) -> None:
        ui = FakeUI()
        FakeStatus.instances = []
        renderer = BigCodeStreamRenderer(ui)  # type: ignore[arg-type]

        with patch("bigcode.ui.renderer.Status", FakeStatus), patch("bigcode.ui.renderer.Text", FakeText):
            renderer.handle(StreamEvent("sess", "checking"))
            renderer.handle(StatusEvent("sess", "model_tool_call_started", metadata={"tool_name": "WebSearch"}))
            renderer.handle(ToolStarted("sess", "toolu_1", "WebSearch"))
            renderer.handle(ToolCompleted("sess", "toolu_1", "WebSearch", False, 1))
            renderer.handle(StreamEvent("sess", " done"))
            renderer.handle(TurnCompleted("sess", "done", "end_turn", 1))

        self.assertEqual([call for call in ui.calls if call[0] == "marker"], [("marker", None, "dim"), ("marker", None, "dim")])
        self.assertEqual([call for call in ui.calls if call[0] == "stream"], [("stream", "checking", None), ("stream", " done", None)])
        self.assertEqual([call for call in ui.calls if call[0] == "print"], [("print", (), "\n"), ("print", (), "\n")])
        self.assertEqual([call for call in ui.calls if call[0] == "divider"], [("divider", None, None)])
        self.assertEqual(renderer.assistant_text, "checking done")
        self.assertEqual([status.messages for status in FakeStatus.instances], [["Thinking... calling WebSearch", "Running WebSearch...", "Completed WebSearch"]])
        self.assertTrue(FakeStatus.instances[0].stopped)

    def test_renderer_handles_permission_events_after_streamed_text(self) -> None:
        ui = FakeUI()
        FakeStatus.instances = []
        renderer = BigCodeStreamRenderer(ui)  # type: ignore[arg-type]

        with patch("bigcode.ui.renderer.Status", FakeStatus), patch("bigcode.ui.renderer.Text", FakeText):
            renderer.handle(StreamEvent("sess", "checking"))
            renderer.handle(PermissionRequested("sess", "toolu_1", "WebSearch", "Approve WebSearch?"))
            renderer.handle(PermissionResolved("sess", "toolu_1", "WebSearch", True, "user"))
            renderer.handle(TurnCompleted("sess", "approved", "end_turn", 0))

        self.assertEqual(renderer.assistant_text, "checking")
        self.assertEqual([call for call in ui.calls if call[0] == "marker"], [("marker", None, "dim")])
        self.assertEqual([call for call in ui.calls if call[0] == "stream"], [("stream", "checking", None)])
        self.assertIn(("divider", None, None), ui.calls)
        self.assertEqual([status.messages for status in FakeStatus.instances], [["Permission approved: WebSearch"]])
        self.assertTrue(FakeStatus.instances[0].stopped)

    def test_renderer_marks_direct_final_answer_without_intermediate_echo(self) -> None:
        ui = FakeUI()
        renderer = BigCodeStreamRenderer(ui)  # type: ignore[arg-type]

        renderer.handle(StreamEvent("sess", "final"))
        renderer.handle(StreamEvent("sess", " answer"))
        renderer.handle(TurnCompleted("sess", "final answer", "end_turn", 0))

        self.assertEqual([call for call in ui.calls if call[0] == "marker"], [("marker", None, "dim")])
        self.assertEqual([call for call in ui.calls if call[0] == "stream"], [("stream", "final", None), ("stream", " answer", None)])
        self.assertEqual([call for call in ui.calls if call[0] == "divider"], [("divider", None, None)])
        self.assertEqual(renderer.assistant_text, "final answer")

    def test_exit_plan_mode_uses_approval_callback(self) -> None:
        calls: list[str] = []

        async def approve(line: str) -> bool:
            calls.append(line)
            return True

        with tempfile.TemporaryDirectory() as td:
            ctx = _make_plan_context(Path(td), approve)
            result = asyncio.run(ExitPlanModeTool().call(ExitPlanModeInput(), ctx))

        self.assertEqual(result.data, {"approved": True, "active": False})
        self.assertEqual(len(calls), 1)
        self.assertIn("Plan Approval Request", calls[0])
        self.assertIn("approved plan", calls[0])
        self.assertFalse(ctx.plan_state.active)
        self.assertTrue(ctx.plan_state.has_exited_plan_mode)
        self.assertEqual(ctx.permission_context.mode, "default")

    def test_exit_plan_mode_keeps_plan_active_when_callback_denies(self) -> None:
        async def deny(line: str) -> bool:
            return False

        with tempfile.TemporaryDirectory() as td:
            ctx = _make_plan_context(Path(td), deny)
            result = asyncio.run(ExitPlanModeTool().call(ExitPlanModeInput(), ctx))

        self.assertEqual(result.data, {"approved": False, "active": True})
        self.assertTrue(ctx.plan_state.active)
        self.assertFalse(ctx.plan_state.has_exited_plan_mode)
        self.assertEqual(ctx.permission_context.mode, "plan")

    def test_ask_user_question_uses_terminal_interaction_callback(self) -> None:
        callbacks: list[object] = []

        async def terminal_interaction(callback):
            callbacks.append(callback)
            return [{"question": "Pick?", "kind": "single", "answer": "A"}]

        with tempfile.TemporaryDirectory() as td:
            ctx = _make_plan_context(Path(td), None)
            ctx.terminal_interaction_callback = terminal_interaction
            result = asyncio.run(
                AskUserQuestionTool().call(
                    AskUserQuestionInput(questions=[UserQuestion(question="Pick?", options=[{"label": "A"}])]),
                    ctx,
                )
            )

        self.assertEqual(result.data, {"answers": [{"question": "Pick?", "kind": "single", "answer": "A"}]})
        self.assertEqual(len(callbacks), 1)

    def test_run_turn_cancellation_cleans_up_and_does_not_bubble(self) -> None:
        class FakeSession:
            session_id = "sess"
            model_ref = "model"

            def __init__(self, started: asyncio.Event) -> None:
                self.abort_event = Event()
                self.started = started

            async def run_turn_stream(self, prompt: str):
                yield StatusEvent("sess", "turn_started")
                self.started.set()
                await asyncio.sleep(10)

        async def run_and_cancel() -> tuple[FakeSession, list[FakeStatus]]:
            started = asyncio.Event()
            session = FakeSession(started)
            repl = BigCodeRepl(session, ui=FakeUI())  # type: ignore[arg-type]
            FakeStatus.instances = []
            with patch("bigcode.ui.renderer.Status", FakeStatus), patch("bigcode.ui.renderer.Text", FakeText):
                task = asyncio.create_task(repl.run_turn("hello", allow_escape_cancel=False))
                await asyncio.wait_for(started.wait(), timeout=1)
                await asyncio.sleep(0)
                task.cancel()
                await task
            return session, FakeStatus.instances

        session, statuses = asyncio.run(run_and_cancel())

        self.assertFalse(session.abort_event.is_set())
        self.assertTrue(statuses)
        self.assertTrue(statuses[0].stopped)

    def test_tty_loop_restores_terminal_after_exit_command(self) -> None:
        class FakeSession:
            session_id = "sess"
            model_ref = "model"

            def __init__(self) -> None:
                self.config = type("Config", (), {"project_state_dir": Path("/tmp")})()

        restored: list[bool] = []
        FakePromptUI.lines = ["/exit"]
        repl = BigCodeRepl(FakeSession(), ui=FakeUI())  # type: ignore[arg-type]

        with patch("bigcode.ui.repl.BigCodePromptUI", FakePromptUI), patch(
            "bigcode.ui.repl._capture_terminal_restore", lambda **kwargs: lambda: restored.append(True)
        ):
            asyncio.run(repl._run_tty_loop())

        self.assertEqual(restored, [True])

    def test_tty_loop_restores_terminal_after_keyboard_interrupt(self) -> None:
        class FakeSession:
            session_id = "sess"
            model_ref = "model"

            def __init__(self) -> None:
                self.config = type("Config", (), {"project_state_dir": Path("/tmp")})()

        restored: list[bool] = []
        FakePromptUI.lines = [KeyboardInterrupt()]
        repl = BigCodeRepl(FakeSession(), ui=FakeUI())  # type: ignore[arg-type]

        with patch("bigcode.ui.repl.BigCodePromptUI", FakePromptUI), patch(
            "bigcode.ui.repl._capture_terminal_restore", lambda **kwargs: lambda: restored.append(True)
        ):
            asyncio.run(repl._run_tty_loop())

        self.assertEqual(restored, [True])

    def test_sane_terminal_settings_force_echo_and_canonical_mode(self) -> None:
        class FakeTermios:
            ECHO = 0b000001
            ICANON = 0b000010
            ISIG = 0b000100
            IEXTEN = 0b001000
            ICRNL = 0b010000
            OPOST = 0b100000

        settings = [0, 0, 0, 0, 0, 0, []]
        sane = _sane_terminal_settings(FakeTermios, settings)

        self.assertEqual(sane[0] & FakeTermios.ICRNL, FakeTermios.ICRNL)
        self.assertEqual(sane[1] & FakeTermios.OPOST, FakeTermios.OPOST)
        self.assertEqual(sane[3] & FakeTermios.ECHO, FakeTermios.ECHO)
        self.assertEqual(sane[3] & FakeTermios.ICANON, FakeTermios.ICANON)
        self.assertEqual(sane[3] & FakeTermios.ISIG, FakeTermios.ISIG)


def _make_plan_context(root: Path, approval_callback) -> ToolExecutionContext:
    plan_store = PlanStore(root / "plans")
    plan_store.write("sess", "approved plan")
    return ToolExecutionContext(
        cwd=root,
        workspace_roots=[root],
        permission_context=ToolPermissionContext(mode="plan", should_avoid_permission_prompts=True),
        read_file_state=ReadFileState(),
        abort_event=Event(),
        session_id="sess",
        is_non_interactive_session=False,
        plan_state=PlanModeState(active=True, pre_plan_permission_mode="default"),
        plan_store=plan_store,
        approval_callback=approval_callback,
    )
