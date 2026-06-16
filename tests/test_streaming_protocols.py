from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bigcode.agent.events import StreamEvent as AgentStreamEvent
from bigcode.agent.session import AgentSession
from bigcode.config import load_runtime_config
from bigcode.context.messages import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage
from bigcode.context.normalizer import normalize_messages_for_api
from bigcode.models.events import StreamEnd, TextDelta


class StreamingProtocolTests(unittest.TestCase):
    def test_new_model_config_selects_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = root / ".bigcode"
            cfg.mkdir()
            (cfg / "models.json").write_text(
                """
                {
                  "defaults": {"model": "openai-main"},
                  "providers": {
                    "openai-local": {
                      "protocol": "openai",
                      "base_url": "https://api.example.test/v1",
                      "api_key_env": "OPENAI_API_KEY"
                    }
                  },
                  "models": {
                    "openai-main": {
                      "provider": "openai-local",
                      "model": "gpt-test",
                      "max_tokens": 8192,
                      "temperature": 0.2,
                      "context_window": 128000
                    }
                  }
                }
                """,
                encoding="utf-8",
            )

            config = load_runtime_config(root, env={"BIGCODE_HOME": str(root / "home")})

            self.assertEqual(config.default_model_ref, "openai-main")
            model = config.models["openai-main"]
            self.assertEqual(model.protocol, "openai")
            self.assertEqual(model.model_id, "gpt-test")
            self.assertEqual(model.max_output_tokens, 8192)
            self.assertEqual(model.temperature, 0.2)

    def test_openai_projection_serializes_tool_history(self) -> None:
        projected = normalize_messages_for_api(
            "system",
            [
                UserMessage("hello"),
                AssistantMessage([ToolUseBlock(id="call_1", name="Read", input={"file_path": "README.md"})]),
                UserMessage([ToolResultBlock(tool_use_id="call_1", content={"ok": True}, is_error=False)], is_meta=True, origin="tool"),
            ],
            protocol="openai",
        )

        self.assertEqual(projected[0], {"role": "user", "content": "hello"})
        self.assertEqual(projected[1]["type"], "function_call")
        self.assertEqual(projected[1]["call_id"], "call_1")
        self.assertIn("README.md", projected[1]["arguments"])
        self.assertEqual(projected[2]["type"], "function_call_output")
        self.assertEqual(projected[2]["call_id"], "call_1")

    def test_session_collects_streaming_text_and_emits_deltas(self) -> None:
        class FakeStreamClient:
            async def stream(self, system_prompt: str, messages: list[Any], tools: list[dict[str, Any]]):
                yield TextDelta("he")
                yield TextDelta("llo")
                yield StreamEnd("end_turn", 3, 2)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            home = Path(td) / "home"
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
            events: list[object] = []
            session = AgentSession(config, session_id="sess_stream", non_interactive=True, event_sink=events.append)

            with patch("bigcode.agent.session.create_client", lambda model: FakeStreamClient()):
                result = asyncio.run(session.run_turn("hello"))

            self.assertEqual(result.assistant_text, "hello")
            self.assertEqual([event.text for event in events if isinstance(event, AgentStreamEvent)], ["he", "llo"])


if __name__ == "__main__":
    unittest.main()
