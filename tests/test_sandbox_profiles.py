from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from bigcode.config import load_runtime_config
from bigcode.tools.subagents.Agent import AgentTool, AgentToolInput
from bigcode.tools.subagents.TaskOutput import TaskOutputInput, TaskOutputTool
from bigcode.tools.subagents.TaskStop import TaskStopInput, TaskStopTool
from bigcode.tools.base import ToolExecutionContext
from bigcode.tools.permissions import ToolPermissionContext, decide_permission
from bigcode.tools.read_file_state import ReadFileState
from bigcode.tools.read.Read import ReadInput, ReadTool
from bigcode.tools.web_fetch.WebFetch import WebFetchInput, WebFetchTool
from bigcode.tools.write.Write import WriteInput, WriteTool
from bigcode.tools.bash.Bash import BashInput, BashTool
from threading import Event


class SandboxProfileTests(unittest.TestCase):
    def make_ctx(self, root: Path, *, mode: str = "default", sandbox_profile: str = "none") -> ToolExecutionContext:
        return ToolExecutionContext(
            cwd=root,
            workspace_roots=[root.resolve()],
            permission_context=ToolPermissionContext(mode=mode, should_avoid_permission_prompts=True),
            read_file_state=ReadFileState(),
            abort_event=Event(),
            session_id="sandbox-test",
            is_non_interactive_session=True,
            sandbox_profile=sandbox_profile,
        )

    def test_config_parses_sandbox_profile_and_cli_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_dir = root / ".bigcode"
            cfg_dir.mkdir()
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
            (cfg_dir / "settings.json").write_text('{"sandbox": {"profile": "read-only"}}', encoding="utf-8")
            config = load_runtime_config(root, cli_overrides={"sandbox": {"profile": "workspace"}})
            self.assertEqual(config.sandbox_profile, "workspace")

    def test_config_warns_on_invalid_sandbox_profile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_dir = root / ".bigcode"
            cfg_dir.mkdir()
            (cfg_dir / "settings.json").write_text('{"sandbox": {"profile": "broken"}}', encoding="utf-8")
            config = load_runtime_config(root)
            self.assertEqual(config.sandbox_profile, "none")
            self.assertTrue(any("invalid sandbox profile" in err for err in config.config_errors))

    def test_read_only_profile_denies_writes_and_general_agent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "a.txt").write_text("hello\n", encoding="utf-8")
            ctx = self.make_ctx(root, mode="bypassPermissions", sandbox_profile="read-only")

            read_decision = asyncio.run(decide_permission(ReadTool(), ReadInput(file_path="a.txt"), ctx))
            task_output_decision = asyncio.run(decide_permission(TaskOutputTool(), TaskOutputInput(), ctx))
            write_decision = asyncio.run(decide_permission(WriteTool(), WriteInput(file_path="a.txt", content="x"), ctx))
            stop_decision = asyncio.run(decide_permission(TaskStopTool(), TaskStopInput(agent_id="subagent_1"), ctx))
            explorer_decision = asyncio.run(decide_permission(AgentTool(), AgentToolInput(prompt="inspect", subagent_type="explorer"), ctx))
            general_decision = asyncio.run(decide_permission(AgentTool(), AgentToolInput(prompt="edit", subagent_type="general-purpose"), ctx))
            bash_read = asyncio.run(decide_permission(BashTool(), BashInput(command="pwd"), ctx))
            bash_mutate = asyncio.run(decide_permission(BashTool(), BashInput(command="touch x.txt"), ctx))

            self.assertEqual(read_decision.behavior, "allow")
            self.assertEqual(task_output_decision.behavior, "allow")
            self.assertEqual(explorer_decision.behavior, "allow")
            self.assertEqual(bash_read.behavior, "allow")
            self.assertEqual(write_decision.behavior, "deny")
            self.assertEqual(stop_decision.behavior, "deny")
            self.assertEqual(general_decision.behavior, "deny")
            self.assertEqual(bash_mutate.behavior, "deny")

    def test_workspace_profile_blocks_network_and_mutating_bash_but_keeps_workspace_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root, mode="bypassPermissions", sandbox_profile="workspace")

            write_decision = asyncio.run(decide_permission(WriteTool(), WriteInput(file_path="a.txt", content="x"), ctx))
            web_decision = asyncio.run(decide_permission(WebFetchTool(), WebFetchInput(url="https://example.com"), ctx))
            bash_read = asyncio.run(decide_permission(BashTool(), BashInput(command="pwd"), ctx))
            bash_mutate = asyncio.run(decide_permission(BashTool(), BashInput(command="touch x.txt"), ctx))

            self.assertEqual(write_decision.behavior, "allow")
            self.assertEqual(bash_read.behavior, "allow")
            self.assertEqual(web_decision.behavior, "deny")
            self.assertEqual(bash_mutate.behavior, "deny")


if __name__ == "__main__":
    unittest.main()
