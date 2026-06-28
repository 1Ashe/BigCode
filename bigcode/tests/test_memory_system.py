from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from bigcode.agent.snapshot import SessionSnapshot, cleanup_old_sessions, next_session_id, save_session_snapshot
from bigcode.context.messages import AssistantMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage
from bigcode.context.system_prompt import build_system_prompt
from bigcode.context.transcript import Transcript
from bigcode.memory.auto_memory import MemoryManager


class MemorySystemTests(unittest.TestCase):
    def test_instruction_includes_are_expanded_and_confined(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            root.mkdir()
            (root / ".git").mkdir()
            docs = root / "docs"
            docs.mkdir()
            (docs / "style.md").write_text("Use focused tests.\n", encoding="utf-8")
            (root / "BIGCODE.md").write_text(
                "# Project\n@include ./docs/style.md\n@include ../outside.md\n@include ./missing.md\n",
                encoding="utf-8",
            )
            (Path(td) / "outside.md").write_text("outside\n", encoding="utf-8")

            prompt = build_system_prompt(
                cwd=root,
                repo_root=root,
                tool_names=[],
                instruction_paths=[root / "BIGCODE.md"],
            ).render()

        self.assertIn("Use focused tests.", prompt)
        self.assertIn("<!-- @include blocked: path outside project -->", prompt)
        self.assertIn("<!-- @include skipped: file not found -->", prompt)

    def test_transcript_load_truncates_incomplete_tool_chain(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            transcript = Transcript(Path(td) / "sess.jsonl")
            transcript.append(UserMessage("hello"))
            transcript.append(
                AssistantMessage(
                    [
                        TextBlock(text="checking"),
                        ToolUseBlock(id="toolu_1", name="Read", input={"file_path": "a.py"}),
                    ]
                )
            )

            loaded = transcript.load()

        self.assertEqual(len(loaded), 1)
        self.assertIsInstance(loaded[0], UserMessage)

    def test_transcript_load_keeps_complete_tool_chain_and_skips_bad_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sess.jsonl"
            transcript = Transcript(path)
            transcript.append(UserMessage("hello"))
            transcript.append(AssistantMessage([ToolUseBlock(id="toolu_1", name="Read", input={})]))
            transcript.append(UserMessage([ToolResultBlock(tool_use_id="toolu_1", content="ok")], is_meta=True, origin="tool"))
            with path.open("a", encoding="utf-8") as f:
                f.write("{bad json\n")

            loaded = transcript.load()

        self.assertEqual(len(loaded), 3)
        self.assertIsInstance(loaded[-1], UserMessage)

    def test_memory_actions_write_scoped_files_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = MemoryManager(
                user_memory_dir=root / "home" / "memory",
                project_memory_dir=root / "repo" / ".bigcode" / "memory",
            )
            manager.apply_actions(
                [
                    {
                        "action": "create",
                        "type": "user",
                        "name": "prefer-spaces",
                        "title": "Prefer spaces",
                        "description": "User prefers four-space indentation",
                        "body": "Use four spaces for indentation.",
                    },
                    {
                        "action": "create",
                        "type": "project",
                        "name": "ci",
                        "title": "CI",
                        "description": "Project uses GitHub Actions",
                        "body": "CI lives in .github/workflows.",
                    },
                ]
            )

            user_index = (root / "home" / "memory" / "MEMORY.md").read_text(encoding="utf-8")
            user_memory = (root / "home" / "memory" / "prefer-spaces.md").read_text(encoding="utf-8")
            project_index = (root / "repo" / ".bigcode" / "memory" / "MEMORY.md").read_text(encoding="utf-8")

        self.assertIn("[Prefer spaces](prefer-spaces.md)", user_index)
        self.assertIn("type: user", user_memory)
        self.assertIn("[CI](ci.md)", project_index)

    def test_memory_index_is_truncated_for_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            user_dir = root / "memory"
            user_dir.mkdir()
            (user_dir / "MEMORY.md").write_text("\n".join(f"- item {i}" for i in range(250)), encoding="utf-8")
            manager = MemoryManager(user_memory_dir=user_dir, project_memory_dir=root / "project-memory")

            content = manager.load_index_for_prompt()

        self.assertIn("[Long-term memory index truncated]", content)
        self.assertLessEqual(len(content.splitlines()), 203)

    def test_cleanup_old_sessions_removes_old_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            old_transcript = state / "transcripts" / "old.jsonl"
            new_transcript = state / "transcripts" / "new.jsonl"
            old_transcript.parent.mkdir(parents=True)
            old_transcript.write_text("{}\n", encoding="utf-8")
            new_transcript.write_text("{}\n", encoding="utf-8")
            old_snapshot = SessionSnapshot(
                session_id="old",
                cwd="",
                repo_root="",
                model=None,
                permission_mode="default",
                task_list_id="old",
                transcript_path=str(old_transcript),
                message_count=1,
                updated_at=time.time() - 31 * 24 * 60 * 60,
            )
            new_snapshot = SessionSnapshot(
                session_id="new",
                cwd="",
                repo_root="",
                model=None,
                permission_mode="default",
                task_list_id="new",
                transcript_path=str(new_transcript),
                message_count=1,
                updated_at=time.time(),
            )
            old_path = save_session_snapshot(state, old_snapshot)
            new_path = save_session_snapshot(state, new_snapshot)
            old_time = time.time() - 31 * 24 * 60 * 60
            os.utime(old_path, (old_time, old_time))

            data = json.loads(old_path.read_text(encoding="utf-8"))
            data["updated_at"] = old_time
            old_path.write_text(json.dumps(data), encoding="utf-8")
            cleanup_old_sessions(state)

            self.assertFalse(old_path.exists())
            self.assertFalse(old_transcript.exists())
            self.assertTrue(new_path.exists())
            self.assertTrue(new_transcript.exists())

    def test_next_session_id_is_monotonic_across_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            (state / "transcripts").mkdir(parents=True)
            (state / "transcripts" / "session_000007.jsonl").write_text("{}\n", encoding="utf-8")

            self.assertEqual(next_session_id(state), "session_000008")
            self.assertEqual(next_session_id(state), "session_000009")

            counter = (state / "session_counter").read_text(encoding="utf-8")
            self.assertEqual(counter, "9")


if __name__ == "__main__":
    unittest.main()
