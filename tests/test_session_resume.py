from __future__ import annotations

import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import BaseModel

from bigcode.agent.session import AgentSession
from bigcode.agent.snapshot import SessionSnapshot, load_session_snapshot, save_session_snapshot
from bigcode.cli import main
from bigcode.config import load_runtime_config
from bigcode.context.messages import AssistantMessage, TextBlock
from bigcode.models.claude_compatible import ModelResponse
from bigcode.skills.loader import load_skills
from bigcode.tools.base import BaseTool, ToolExecutionContext, ToolResult
from bigcode.tools.bash.Bash import BashInput
from bigcode.tools.read.Read import ReadInput, ReadTool
from bigcode.tools.registry import ToolRegistry
from bigcode.tools.runner import ToolRunner, ToolUse


class SessionResumeTests(unittest.TestCase):
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

    def test_run_turn_writes_transcript_and_snapshot(self) -> None:
        class FakeClient:
            def __init__(self, model) -> None:
                self.model = model

            async def complete(self, system_prompt, messages, tools):
                return ModelResponse(AssistantMessage([TextBlock(text="done")], model=self.model.ref, stop_reason="end_turn"), raw={})

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            config = self.make_config(root, home)
            session = AgentSession(config, session_id="sess_a", non_interactive=True)
            with patch("bigcode.agent.session.ClaudeCompatibleModelClient", FakeClient):
                result = asyncio.run(session.run_turn("hello"))

            self.assertEqual(result.assistant_text, "done")
            self.assertTrue((config.project_state_dir / "transcripts" / "sess_a.jsonl").exists())
            snapshot = load_session_snapshot(config.project_state_dir, "sess_a")
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.message_count, 2)
            self.assertEqual(snapshot.model, "local:test")

    def test_resume_loads_transcript_and_model_override_wins(self) -> None:
        class FakeClient:
            def __init__(self, model) -> None:
                self.model = model

            async def complete(self, system_prompt, messages, tools):
                return ModelResponse(AssistantMessage([TextBlock(text="remembered")], model=self.model.ref), raw={})

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            config = self.make_config(root, home)
            session = AgentSession(config, session_id="sess_b", non_interactive=True)
            with patch("bigcode.agent.session.ClaudeCompatibleModelClient", FakeClient):
                asyncio.run(session.run_turn("hello"))

            resumed = AgentSession(config, session_id="sess_b", model_ref="local:test", non_interactive=True)
            self.assertEqual(len(resumed.messages), 2)
            self.assertEqual(resumed.model_ref, "local:test")

    def test_read_file_state_snapshot_blocks_duplicate_until_disk_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            path = root / "a.txt"
            path.write_text("hello\n", encoding="utf-8")
            config = self.make_config(root, home)
            session = AgentSession(config, session_id="sess_read", non_interactive=True)
            first = asyncio.run(ReadTool().call(ReadInput(file_path="a.txt"), session.make_tool_context()))
            self.assertEqual(first.data["type"], "text")
            session._save_snapshot()

            resumed = AgentSession(config, session_id="sess_read", non_interactive=True)
            second = asyncio.run(ReadTool().call(ReadInput(file_path="a.txt"), resumed.make_tool_context()))
            self.assertEqual(second.data["type"], "file_unchanged")
            path.write_text("changed\n", encoding="utf-8")
            third = asyncio.run(ReadTool().call(ReadInput(file_path="a.txt"), resumed.make_tool_context()))
            self.assertEqual(third.data["type"], "text")
            self.assertIn("changed", third.data["content"])

    def test_resume_without_id_lists_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            save_session_snapshot(
                state,
                SessionSnapshot(
                    session_id="sess_list",
                    cwd="/tmp/work",
                    repo_root="/tmp/work",
                    model="local:test",
                    permission_mode="default",
                    task_list_id="sess_list",
                    transcript_path=str(state / "transcripts" / "sess_list.jsonl"),
                    message_count=3,
                    active_artifacts={"toolu_1": {"artifact_id": "toolu_1"}},
                ),
            )

            def fail_session(*args, **kwargs):
                raise AssertionError("resume list should not create AgentSession")

            out = io.StringIO()
            with patch("bigcode.cli.load_runtime_config", lambda path: SimpleNamespace(project_state_dir=state)):
                with patch("bigcode.cli.AgentSession", fail_session):
                    with contextlib.redirect_stdout(out):
                        main(["resume", "--cwd", str(Path(td))])
            rendered = out.getvalue()
            self.assertIn("sess_list", rendered)
            self.assertIn("messages", rendered)
            self.assertIn("artifacts", rendered)

    def test_skill_load_and_bash_verification_update_snapshot(self) -> None:
        class FakeBashTool(BaseTool[BashInput, dict]):
            name = "Bash"
            description = "Fake bash."
            input_model = BashInput
            permission_category = "read"
            state_effect = "none"

            async def call(self, input: BashInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
                return ToolResult({"command": input.command, "exit_code": 0, "stdout": "ok", "stderr": ""})

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            skill = root / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo\n---\nUse me.", encoding="utf-8")
            config = self.make_config(root, home)
            session = AgentSession(config, session_id="sess_meta", non_interactive=True)
            session.permission_context.mode = "bypassPermissions"
            session.skill_registry = load_skills([root / "skills"])

            asyncio.run(session.runner.run_one(ToolUse("toolu_skill", "SkillLoad", {"name": "demo"}), session.make_tool_context()))
            registry = ToolRegistry()
            registry.register(FakeBashTool())
            asyncio.run(ToolRunner(registry).run_one(ToolUse("toolu_bash", "Bash", {"command": "npm test"}), session.make_tool_context()))

            snapshot = load_session_snapshot(config.project_state_dir, "sess_meta")
            self.assertIn("demo", snapshot.loaded_skills)
            self.assertEqual(snapshot.last_verification["command"], "npm test")
            self.assertEqual(snapshot.last_verification["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
