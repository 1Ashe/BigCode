from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bigcode.agent import JsonlEventSink, StatusEvent, TurnCompleted
from bigcode.agent.gateway import EVENT_SCHEMA_VERSION, serialize_agent_event
from bigcode.agent.session import AgentSession
from bigcode.cli import main
from bigcode.config import load_runtime_config
from bigcode.context.messages import AssistantMessage, TextBlock
from bigcode.models.claude_compatible import ModelResponse


class Phase5GatewayTests(unittest.TestCase):
    def make_config(self, root: Path, home: Path):
        cfg_dir = root / ".bigcode"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "models.json").write_text(
            """
            {
              "default_model": "local:test",
              "providers": {
                "local": {
                  "base_url": "https://api.example.test/v1",
                  "models": {"test": {"id": "test-model"}}
                }
              }
            }
            """,
            encoding="utf-8",
        )
        return load_runtime_config(root, env={"BIGCODE_HOME": str(home)})

    def test_serialize_agent_event_adds_schema_version(self) -> None:
        payload = serialize_agent_event(StatusEvent(session_id="sess", status="started"))
        self.assertEqual(payload["schema_version"], EVENT_SCHEMA_VERSION)
        self.assertEqual(payload["event_type"], "status")

    def test_jsonl_event_sink_writes_jsonl(self) -> None:
        stream = io.StringIO()
        sink = JsonlEventSink(stream)
        sink(StatusEvent(session_id="sess", status="started"))
        sink(TurnCompleted(session_id="sess", assistant_text="done", stop_reason="end_turn", tool_result_count=2))
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["schema_version"], EVENT_SCHEMA_VERSION)
        self.assertEqual(rows[1]["event_type"], "turn_completed")
        self.assertEqual(rows[1]["assistant_text"], "done")

    def test_session_emits_turn_completed_event(self) -> None:
        class FakeClient:
            def __init__(self, model) -> None:
                self.model = model

            async def complete(self, system_prompt, messages, tools):
                return ModelResponse(AssistantMessage([TextBlock(text="done")], model=self.model.ref, stop_reason="end_turn"), raw={})

        with tempfile.TemporaryDirectory() as td, patch("bigcode.agent.session.ClaudeCompatibleModelClient", FakeClient):
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            config = self.make_config(root, home)
            events: list[object] = []
            session = AgentSession(config, session_id="sess_events", non_interactive=True, event_sink=events.append)
            result = asyncio.run(session.run_turn("hello"))
            self.assertEqual(result.assistant_text, "done")
            self.assertTrue(any(isinstance(event, TurnCompleted) and event.assistant_text == "done" for event in events))

    def test_cli_run_jsonl_stream_suppresses_plain_output(self) -> None:
        calls: list[dict] = []

        class DummySession:
            def __init__(self, *args, **kwargs) -> None:
                calls.append(kwargs)
                self.event_sink = kwargs.get("event_sink")

            async def start(self) -> None:
                if self.event_sink:
                    self.event_sink(StatusEvent(session_id="sess_cli", status="session_started"))

            async def run_turn(self, prompt: str):
                if self.event_sink:
                    self.event_sink(TurnCompleted(session_id="sess_cli", assistant_text="plain answer", stop_reason="end_turn", tool_result_count=0))
                return SimpleNamespace(assistant_text="plain answer")

        with tempfile.TemporaryDirectory() as td:
            out = io.StringIO()
            with patch("bigcode.cli.load_runtime_config", lambda path, **kwargs: object()), patch("bigcode.cli.AgentSession", DummySession):
                with contextlib.redirect_stdout(out):
                    main(["run", "hello", "--cwd", td, "--event-stream", "jsonl"])
            rows = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual([row["event_type"] for row in rows], ["status", "turn_completed"])
            self.assertIsNotNone(calls[0]["event_sink"])


if __name__ == "__main__":
    unittest.main()
