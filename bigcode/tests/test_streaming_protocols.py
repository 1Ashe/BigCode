from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bigcode.agent.events import StreamEvent, ToolCompleted, ToolStarted, TurnCompleted
from bigcode.agent.session import AgentSession
from bigcode.config import load_runtime_config
from bigcode.models.events import StreamEnd, TextDelta, ToolCallComplete, ToolCallStart


class SessionStreamingTests(unittest.TestCase):
    def make_session(self, temp_dir: str, *, session_id: str) -> AgentSession:
        root = Path(temp_dir) / "repo"
        home = Path(temp_dir) / "home"
        root.mkdir()
        cfg = root / ".bigcode"
        cfg.mkdir()
        (cfg / "models.json").write_text(
            """
            {
              "default_model": "local:test",
              "providers": {
                "local": {
                  "protocol": "anthropic",
                  "base_url": "https://api.example.test/v1",
                  "models": {"test": {"id": "test-model", "context_window": 128000}}
                }
              }
            }
            """,
            encoding="utf-8",
        )
        config = load_runtime_config(root, env={"BIGCODE_HOME": str(home)})
        return AgentSession(config, session_id=session_id, non_interactive=True)

    def make_config(self, temp_dir: str):
        root = Path(temp_dir) / "repo"
        home = Path(temp_dir) / "home"
        root.mkdir()
        cfg = root / ".bigcode"
        cfg.mkdir()
        (cfg / "models.json").write_text(
            """
            {
              "default_model": "local:test",
              "providers": {
                "local": {
                  "protocol": "anthropic",
                  "base_url": "https://api.example.test/v1",
                  "models": {"test": {"id": "test-model", "context_window": 128000}}
                }
              }
            }
            """,
            encoding="utf-8",
        )
        return load_runtime_config(root, env={"BIGCODE_HOME": str(home)})

    def test_new_sessions_use_incrementing_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(td)
            first = AgentSession(config, non_interactive=True)
            second = AgentSession(config, non_interactive=True)

        self.assertEqual(first.session_id, "session_000001")
        self.assertEqual(second.session_id, "session_000002")

    def test_explicit_session_id_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(td)
            session = AgentSession(config, session_id="sess_stream", non_interactive=True)

        self.assertEqual(session.session_id, "sess_stream")

    def test_text_deltas_are_emitted_before_turn_completion(self) -> None:
        class FakeStreamClient:
            async def stream(self, system_prompt: str, messages: list[Any], tools: list[dict[str, Any]]):
                yield TextDelta("a")
                yield TextDelta("b")
                yield StreamEnd("end_turn", 3, 2)

        with tempfile.TemporaryDirectory() as td:
            session = self.make_session(td, session_id="sess_stream")

            with patch("bigcode.agent.session.create_client", lambda model: FakeStreamClient()):
                events = asyncio.run(_collect_events(session.run_turn_stream("hello")))

        stream_indexes = [idx for idx, event in enumerate(events) if isinstance(event, StreamEvent)]
        completed_index = next(idx for idx, event in enumerate(events) if isinstance(event, TurnCompleted))
        completed = events[completed_index]

        self.assertEqual([events[idx].text for idx in stream_indexes], ["a", "b"])
        self.assertTrue(all(idx < completed_index for idx in stream_indexes))
        self.assertEqual(completed.assistant_text, "ab")

    def test_text_deltas_are_emitted_before_tool_events(self) -> None:
        class FakeStreamClient:
            async def stream(self, system_prompt: str, messages: list[Any], tools: list[dict[str, Any]]):
                yield TextDelta("checking")
                yield ToolCallStart(id="toolu_1", name="MissingTool")
                yield ToolCallComplete(id="toolu_1", name="MissingTool", input={})
                yield StreamEnd("tool_use", 3, 2)

        with tempfile.TemporaryDirectory() as td:
            session = self.make_session(td, session_id="sess_tool_stream")

            with patch("bigcode.agent.session.create_client", lambda model: FakeStreamClient()):
                events = asyncio.run(_collect_events(session.run_turn_stream("hello", max_steps=1)))

        stream_index = next(idx for idx, event in enumerate(events) if isinstance(event, StreamEvent))
        tool_started_index = next(idx for idx, event in enumerate(events) if isinstance(event, ToolStarted))
        tool_completed_index = next(idx for idx, event in enumerate(events) if isinstance(event, ToolCompleted))
        completed = next(event for event in events if isinstance(event, TurnCompleted))

        self.assertEqual(events[stream_index].text, "checking")
        self.assertLess(stream_index, tool_started_index)
        self.assertLess(stream_index, tool_completed_index)
        self.assertEqual(completed.assistant_text, "checking")


async def _collect_events(stream: Any) -> list[object]:
    events: list[object] = []
    async for event in stream:
        events.append(event)
    return events
