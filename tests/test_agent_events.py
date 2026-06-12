from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import BaseModel

from bigcode.agent.events import ErrorEvent, ToolCompleted, ToolStarted
from bigcode.cli import main
from bigcode.tools.base import BaseTool, ToolExecutionContext, ToolResult
from bigcode.tools.permissions import ToolPermissionContext
from bigcode.tools.read_file_state import ReadFileState
from bigcode.tools.registry import ToolRegistry
from bigcode.tools.runner import ToolRunner, ToolUse


class AgentEventsTests(unittest.TestCase):
    def make_ctx(self, root: Path, events: list[object]) -> ToolExecutionContext:
        return ToolExecutionContext(
            cwd=root,
            workspace_roots=[root],
            permission_context=ToolPermissionContext(mode="default", should_avoid_permission_prompts=True),
            read_file_state=ReadFileState(),
            abort_event=Event(),
            session_id="session-test",
            is_non_interactive_session=True,
            event_sink=events.append,
        )

    def test_tool_runner_emits_success_events(self) -> None:
        class MarkerInput(BaseModel):
            value: int

        class MarkerTool(BaseTool[MarkerInput, dict]):
            name = "Marker"
            description = "Marker."
            input_model = MarkerInput
            permission_category = "read"
            state_effect = "none"

            async def call(self, input: MarkerInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
                return ToolResult({"value": input.value})

        with tempfile.TemporaryDirectory() as td:
            events: list[object] = []
            registry = ToolRegistry()
            registry.register(MarkerTool())
            result = asyncio.run(ToolRunner(registry).run_one(ToolUse("toolu_1", "Marker", {"value": 3}), self.make_ctx(Path(td), events)))
            self.assertFalse(result.is_error)
            self.assertIsInstance(events[0], ToolStarted)
            self.assertIsInstance(events[1], ToolCompleted)
            self.assertEqual(events[0].tool_name, "Marker")
            self.assertFalse(events[1].is_error)

    def test_tool_runner_emits_error_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            events: list[object] = []
            result = asyncio.run(ToolRunner(ToolRegistry()).run_one(ToolUse("toolu_1", "Missing", {}), self.make_ctx(Path(td), events)))
            self.assertTrue(result.is_error)
            self.assertIsInstance(events[0], ToolStarted)
            self.assertIsInstance(events[1], ToolCompleted)
            self.assertIsInstance(events[2], ErrorEvent)
            self.assertTrue(events[1].is_error)
            self.assertEqual(events[2].tool_name, "Missing")
            self.assertIn("Unknown tool", events[2].message)

    def test_cli_run_starts_session_before_turn(self) -> None:
        calls: list[object] = []

        class DummySession:
            def __init__(self, *args, **kwargs) -> None:
                calls.append(("init", kwargs))

            async def start(self) -> None:
                calls.append("start")

            async def run_turn(self, prompt: str):
                calls.append(("run", prompt))
                return SimpleNamespace(assistant_text="")

        with tempfile.TemporaryDirectory() as td:
            with patch("bigcode.cli.load_runtime_config", lambda path: object()), patch("bigcode.cli.AgentSession", DummySession):
                main(["run", "hello", "--cwd", td])
        self.assertEqual(calls[1], "start")
        self.assertEqual(calls[2], ("run", "hello"))


if __name__ == "__main__":
    unittest.main()
