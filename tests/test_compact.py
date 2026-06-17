from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bigcode.agent.session import AgentSession
from bigcode.config import load_runtime_config
from bigcode.config.models import CompactConfig
from bigcode.context.attachments import Attachment
from bigcode.context.compact import (
    TIME_BASED_CLEARED_MESSAGE,
    CompactDeps,
    ContextCompactState,
    apply_context_compact,
    build_message_groups,
    estimate_context_tokens,
    replay_compact_records,
)
from bigcode.context.messages import (
    AssistantMessage,
    AttachmentMessage,
    CompactRecordMessage,
    ContextSummaryMessage,
    SystemPromptSnapshotMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from bigcode.context.transcript import Transcript
from bigcode.models.claude_compatible import ModelResponse


class CompactTests(unittest.TestCase):
    def test_attachment_messages_count_in_extra_context_budget(self) -> None:
        deps = CompactDeps(
            extra_context_messages=[
                AttachmentMessage(
                    Attachment(type="context", text="extra context for model", source="hooks")
                )
            ]
        )

        self.assertGreater(estimate_context_tokens([], deps), 0)

    def test_time_microcompact_persists_without_mutating_ui_results(self) -> None:
        messages = []
        for index in range(7):
            messages.extend(
                [
                    AssistantMessage(
                        [ToolUseBlock(id=f"tool-{index}", name="Read", input={"file_path": f"{index}.txt"})],
                        timestamp=time.time() - 7200,
                    ),
                    UserMessage([ToolResultBlock(tool_use_id=f"tool-{index}", content=f"result-{index}")]),
                ]
            )
        deps = CompactDeps(
            config=CompactConfig(snip_enabled=False, auto_compact_enabled=False),
            context_window=100000,
        )

        result = asyncio.run(apply_context_compact(messages, deps))

        self.assertTrue(result.micro_compacted)
        self.assertEqual(result.records_to_append[0].cleared_tool_use_ids, ["tool-0", "tool-1"])
        self.assertEqual(messages[1].content[0].content, "result-0")
        ui_messages = messages + result.records_to_append
        projected = replay_compact_records(ui_messages)
        projected_results = [
            block.content
            for message in projected
            if isinstance(message, UserMessage)
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        self.assertEqual(projected_results[:2], [TIME_BASED_CLEARED_MESSAGE] * 2)
        self.assertEqual(asyncio.run(apply_context_compact(ui_messages, deps)).records_to_append, [])

    def test_snip_covers_complete_tool_group_and_keeps_ui_messages(self) -> None:
        messages = []
        for index in range(12):
            call = AssistantMessage(
                [ToolUseBlock(id=f"read-{index}", name="Read", input={"file_path": f"{index}.txt"})]
            )
            result = UserMessage(
                [ToolResultBlock(tool_use_id=f"read-{index}", content="x" * 500)]
            )
            messages.extend([call, result])
        original_ids = [message.uuid for message in messages]
        deps = CompactDeps(
            config=CompactConfig(
                time_microcompact_enabled=False,
                auto_compact_enabled=False,
                snip_threshold=0.20,
                snip_target=0.10,
                snip_min_messages=4,
                snip_min_tokens=100,
                protected_tail_messages=4,
                protected_tail_tokens=100,
                blocked_threshold=0.99,
            ),
            context_window=4000,
        )

        result = asyncio.run(apply_context_compact(messages, deps))

        self.assertTrue(result.snipped)
        self.assertEqual([message.uuid for message in messages], original_ids)
        covered = set(result.records_to_append[0].covered_message_ids)
        for index in range(0, len(messages), 2):
            pair = {messages[index].uuid, messages[index + 1].uuid}
            self.assertIn(len(pair & covered), {0, 2})

    def test_context_collapse_suppresses_auto_compact(self) -> None:
        calls: list[str] = []

        async def summarize(kind: str, content: str) -> str:
            calls.append(kind)
            return "preserved state"

        messages = self._large_conversation()
        deps = CompactDeps(
            config=CompactConfig(
                time_microcompact_enabled=False,
                snip_enabled=False,
                context_collapse_enabled=True,
                collapse_threshold=0.20,
                collapse_target=0.10,
                collapse_min_tokens_saved=100,
                auto_compact_threshold=0.20,
                protected_tail_messages=4,
                protected_tail_tokens=100,
                blocked_threshold=0.99,
            ),
            context_window=5000,
            summarize=summarize,
        )

        result = asyncio.run(apply_context_compact(messages, deps))

        self.assertGreater(result.collapsed_spans, 0)
        self.assertFalse(result.auto_compacted)
        self.assertTrue(calls)
        self.assertEqual(set(calls), {"collapse"})

    def test_auto_compact_supersedes_prior_summary_record(self) -> None:
        first = UserMessage("old request " + "x" * 500)
        second = AssistantMessage([TextBlock(text="old answer " + "y" * 500)])
        prior = CompactRecordMessage(
            "collapse",
            covered_message_ids=[first.uuid, second.uuid],
            summary="[Collapsed context summary]\nold summary",
        )
        messages = [first, second, prior, *self._large_conversation()]

        async def summarize(kind: str, content: str) -> str:
            return "new complete summary"

        deps = CompactDeps(
            config=CompactConfig(
                time_microcompact_enabled=False,
                snip_enabled=False,
                auto_compact_threshold=0.20,
                auto_keep_tokens=300,
                auto_min_keep_messages=4,
                protected_tail_messages=4,
                protected_tail_tokens=100,
                blocked_threshold=0.99,
            ),
            state=ContextCompactState(step_index=0),
            context_window=5000,
            summarize=summarize,
        )

        result = asyncio.run(apply_context_compact(messages, deps))

        self.assertTrue(result.auto_compacted)
        auto_record = result.records_to_append[0]
        self.assertIn(prior.uuid, auto_record.superseded_record_ids)
        replayed = replay_compact_records(messages + result.records_to_append)
        summaries = [message for message in replayed if isinstance(message, ContextSummaryMessage)]
        self.assertEqual(len(summaries), 1)
        self.assertIn("new complete summary", summaries[0].summary)

    def test_auto_compact_preserves_leading_system_event(self) -> None:
        system = SystemMessage("local boundary", subtype="compact_boundary")

        async def summarize(kind: str, content: str) -> str:
            return "summary after system"

        deps = CompactDeps(
            config=CompactConfig(
                time_microcompact_enabled=False,
                snip_enabled=False,
                auto_compact_threshold=0.20,
                auto_keep_tokens=300,
                auto_min_keep_messages=4,
                blocked_threshold=0.99,
            ),
            context_window=5000,
            summarize=summarize,
        )
        result = asyncio.run(apply_context_compact([system, *self._large_conversation()], deps))
        self.assertTrue(result.auto_compacted)
        self.assertIs(result.projected_messages[0], system)

    def test_blocked_when_pressure_remains_above_limit(self) -> None:
        messages = self._large_conversation()
        deps = CompactDeps(
            config=CompactConfig(
                time_microcompact_enabled=False,
                snip_enabled=False,
                context_collapse_enabled=False,
                auto_compact_enabled=False,
                blocked_threshold=0.50,
            ),
            context_window=1000,
        )
        result = asyncio.run(apply_context_compact(messages, deps))
        self.assertTrue(result.blocked)

    def test_transcript_round_trip_preserves_identity_and_compact_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "session.jsonl"
            transcript = Transcript(path)
            user = UserMessage("hello", uuid="user-fixed", timestamp=123.5)
            record = CompactRecordMessage(
                "snip",
                covered_message_ids=[user.uuid],
                summary="summary",
                uuid="compact-fixed",
                timestamp=456.5,
            )
            prompt = SystemPromptSnapshotMessage("fixed prompt", uuid="prompt-fixed", timestamp=100.0)
            for message in (prompt, user, record):
                transcript.append(message)

            loaded = transcript.load()

            self.assertEqual([message.uuid for message in loaded], ["prompt-fixed", "user-fixed", "compact-fixed"])
            self.assertEqual([message.timestamp for message in loaded], [100.0, 123.5, 456.5])
            self.assertEqual(loaded[2].covered_message_ids, ["user-fixed"])

    def test_out_of_order_tool_result_is_protected_as_orphan(self) -> None:
        result = UserMessage([ToolResultBlock(tool_use_id="late", content="early result")])
        call = AssistantMessage([ToolUseBlock(id="late", name="Read", input={})])
        groups = build_message_groups([result, call])
        self.assertTrue(groups[0].protected)
        self.assertIn("orphan_tool_result", groups[0].reasons)
        self.assertTrue(groups[1].protected)
        self.assertIn("unclosed_tool_call", groups[1].reasons)

    def test_system_prompt_is_frozen_across_turns_and_resume(self) -> None:
        captured: list[str] = []

        class FakeClient:
            def __init__(self, model) -> None:
                self.model = model

            async def complete(self, system_prompt, messages, tools):
                captured.append(system_prompt)
                return ModelResponse(
                    AssistantMessage([TextBlock(text="done")], model=self.model.ref, stop_reason="end_turn"),
                    raw={},
                )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            (root / "BIGCODE.md").write_text("initial instruction", encoding="utf-8")
            config = self._make_config(root, home)
            session = AgentSession(config, session_id="fixed_prompt", non_interactive=True)
            initial = session.system_prompt
            (root / "BIGCODE.md").write_text("changed instruction", encoding="utf-8")

            with patch("bigcode.agent.session.ClaudeCompatibleModelClient", FakeClient):
                asyncio.run(session.run_turn("one"))
                asyncio.run(session.run_turn("two"))

            resumed = AgentSession(config, session_id="fixed_prompt", non_interactive=True)
            self.assertEqual(resumed.system_prompt, initial)
            self.assertEqual(captured, [initial, initial])
            self.assertIn("initial instruction", initial)
            self.assertNotIn("changed instruction", initial)

    def test_manual_compact_appends_record_without_replacing_ui_history(self) -> None:
        class FakeClient:
            def __init__(self, model) -> None:
                self.model = model

            async def complete(self, system_prompt, messages, tools):
                return ModelResponse(
                    AssistantMessage([TextBlock(text="manual summary")], model=self.model.ref),
                    raw={},
                )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            config = self._make_config(root, home)
            config = replace(
                config,
                compact=replace(
                    config.compact,
                    context_collapse_enabled=True,
                    snip_threshold=0.01,
                    auto_keep_tokens=300,
                    auto_min_keep_messages=4,
                ),
            )
            session = AgentSession(config, session_id="manual", non_interactive=True)
            session.messages.extend(self._large_conversation())
            original_ids = {
                message.uuid
                for message in session.messages
                if isinstance(message, (UserMessage, AssistantMessage))
            }

            with patch("bigcode.agent.session.ClaudeCompatibleModelClient", FakeClient):
                asyncio.run(session.handle_command("/compact"))

            remaining_original_ids = {
                message.uuid
                for message in session.messages
                if isinstance(message, (UserMessage, AssistantMessage))
            }
            self.assertEqual(remaining_original_ids, original_ids)
            records = [message for message in session.messages if isinstance(message, CompactRecordMessage)]
            self.assertEqual([record.compact_type for record in records], ["auto"])

    def test_compact_settings_are_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            config_dir = root / ".bigcode"
            config_dir.mkdir()
            (config_dir / "settings.json").write_text(
                """
                {
                  "compact": {
                    "time_microcompact_gap_minutes": 90,
                    "time_microcompact_keep_recent": 7,
                    "context_collapse_enabled": true,
                    "auto_compact_threshold": 0.92
                  }
                }
                """,
                encoding="utf-8",
            )
            config = self._make_config(root, home)
            self.assertEqual(config.compact.time_microcompact_gap_minutes, 90)
            self.assertEqual(config.compact.time_microcompact_keep_recent, 7)
            self.assertTrue(config.compact.context_collapse_enabled)
            self.assertEqual(config.compact.auto_compact_threshold, 0.92)

    def test_auto_compact_failure_counter_trips_without_partial_record(self) -> None:
        async def fail_summary(kind: str, content: str) -> str:
            raise RuntimeError("provider unavailable")

        state = ContextCompactState(step_index=0)
        deps = CompactDeps(
            config=CompactConfig(
                time_microcompact_enabled=False,
                snip_enabled=False,
                auto_compact_threshold=0.20,
                auto_keep_tokens=300,
                auto_min_keep_messages=4,
                auto_max_failures=3,
                blocked_threshold=0.99,
            ),
            state=state,
            context_window=5000,
            summarize=fail_summary,
        )
        messages = self._large_conversation()
        for expected_failures in range(1, 4):
            result = asyncio.run(apply_context_compact(messages, deps))
            self.assertEqual(result.records_to_append, [])
            self.assertEqual(state.auto_compact_failures, expected_failures)
        result = asyncio.run(apply_context_compact(messages, deps))
        self.assertEqual(result.records_to_append, [])
        self.assertEqual(state.auto_compact_failures, 3)

    @staticmethod
    def _large_conversation() -> list:
        messages = []
        for index in range(20):
            messages.extend(
                [
                    UserMessage(f"request-{index} " + "x" * 300),
                    AssistantMessage([TextBlock(text=f"answer-{index} " + "y" * 300)]),
                ]
            )
        return messages

    @staticmethod
    def _make_config(root: Path, home: Path):
        config_dir = root / ".bigcode"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "models.json").write_text(
            """
            {
              "default_model": "local:test",
              "providers": {
                "local": {
                  "base_url": "https://api.example.test/v1",
                  "models": {"test": {"id": "test-model", "context_window": 128000}}
                }
              }
            }
            """,
            encoding="utf-8",
        )
        return load_runtime_config(root, env={"BIGCODE_HOME": str(home)})


if __name__ == "__main__":
    unittest.main()
