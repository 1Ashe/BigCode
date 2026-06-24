from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from bigcode.agent.events import PermissionRequested, PermissionResolved, StreamEvent, ToolCompleted, ToolStarted, TurnCompleted
from bigcode.plan import PlanModeState, PlanStore
from bigcode.tools.base import ToolExecutionContext
from bigcode.tools.permissions import ToolPermissionContext
from bigcode.tools.plan.AskUserQuestion import AskUserQuestionInput, AskUserQuestionTool, UserQuestion
from bigcode.tools.plan.ExitPlanMode import ExitPlanModeInput, ExitPlanModeTool
from bigcode.tools.read_file_state import ReadFileState
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


class UIInteractionTests(unittest.TestCase):
    def test_renderer_keeps_tool_events_after_streamed_text(self) -> None:
        ui = FakeUI()
        FakeStatus.instances = []
        renderer = BigCodeStreamRenderer(ui)  # type: ignore[arg-type]

        with patch("bigcode.ui.renderer.Status", FakeStatus), patch("bigcode.ui.renderer.Text", FakeText):
            renderer.handle(StreamEvent("sess", "checking"))
            renderer.handle(ToolStarted("sess", "toolu_1", "WebSearch"))
            renderer.handle(ToolCompleted("sess", "toolu_1", "WebSearch", False, 1))
            renderer.handle(StreamEvent("sess", " done"))
            renderer.handle(TurnCompleted("sess", "checking done", "end_turn", 1))

        self.assertEqual([call for call in ui.calls if call[0] == "stream"], [("stream", "checking", None), ("stream", " done", None)])
        self.assertEqual([call for call in ui.calls if call[0] == "print"], [("print", (), "\n"), ("print", (), "\n")])
        self.assertEqual(renderer.assistant_text, "checking done")
        self.assertEqual([status.messages for status in FakeStatus.instances], [["Running WebSearch...", "Completed WebSearch"]])
        self.assertTrue(FakeStatus.instances[0].stopped)

    def test_renderer_handles_permission_events_after_streamed_text(self) -> None:
        ui = FakeUI()
        FakeStatus.instances = []
        renderer = BigCodeStreamRenderer(ui)  # type: ignore[arg-type]

        with patch("bigcode.ui.renderer.Status", FakeStatus), patch("bigcode.ui.renderer.Text", FakeText):
            renderer.handle(StreamEvent("sess", "checking"))
            renderer.handle(PermissionRequested("sess", "toolu_1", "WebSearch", "Approve WebSearch?"))
            renderer.handle(PermissionResolved("sess", "toolu_1", "WebSearch", True, "user"))
            renderer.handle(TurnCompleted("sess", "checking", "end_turn", 0))

        self.assertEqual(renderer.assistant_text, "checking")
        self.assertIn(("print", (), "\n"), ui.calls)
        self.assertEqual([status.messages for status in FakeStatus.instances], [["Permission approved: WebSearch"]])
        self.assertTrue(FakeStatus.instances[0].stopped)

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
