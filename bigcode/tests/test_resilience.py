from __future__ import annotations

import asyncio
import ast
import tempfile
import unittest
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch

from bigcode.agent.session import AgentSession
from bigcode.config import load_runtime_config
from bigcode.config.models import McpServerConfig
from bigcode.mcp.client import McpClientManager
from bigcode.tools.bash.Bash import BashInput, BashTool
from bigcode.tools.base import ToolExecutionContext
from bigcode.tools.permissions import ToolPermissionContext
from bigcode.tools.read_file_state import ReadFileState


class ResilienceTests(unittest.TestCase):
    def test_mcp_call_tool_times_out(self) -> None:
        class HangingClient:
            async def call_tool(self, tool_name: str, arguments: dict, raise_on_error: bool = False):
                await asyncio.sleep(10)

        manager = McpClientManager({"slow": McpServerConfig("slow", {"timeout": 0.01})})
        manager._fastmcp_available = True
        manager._clients["slow"] = HangingClient()

        with self.assertRaisesRegex(RuntimeError, "timed out after 0.01s"):
            asyncio.run(manager.call_tool("slow", "Never", {}))

    def test_mcp_close_all_times_out_and_clears_clients(self) -> None:
        class HangingClient:
            async def close(self):
                await asyncio.sleep(10)

        manager = McpClientManager({"slow": McpServerConfig("slow", {"timeout": 0.01})})
        manager._fastmcp_available = True
        manager._clients["slow"] = HangingClient()

        with self.assertRaisesRegex(RuntimeError, "close timed out"):
            asyncio.run(manager.close_all())
        self.assertFalse(manager._clients)

    def test_bash_cancellation_kills_process(self) -> None:
        class FakeProcess:
            returncode = None

            def __init__(self) -> None:
                self.killed = False
                self.waited = False

            async def communicate(self):
                await asyncio.sleep(10)

            def kill(self) -> None:
                self.killed = True

            async def wait(self) -> None:
                self.waited = True

        proc = FakeProcess()

        async def fake_subprocess_shell(*args: Any, **kwargs: Any) -> FakeProcess:
            return proc

        async def run_and_cancel() -> None:
            ctx = _make_tool_context(Path.cwd())
            task = asyncio.create_task(BashTool().call(BashInput(command="sleep 10"), ctx))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with patch("bigcode.tools.bash.Bash.asyncio.create_subprocess_shell", fake_subprocess_shell):
            asyncio.run(run_and_cancel())

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    def test_session_shutdown_cancels_background_tasks_and_closes_mcp(self) -> None:
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
            session = AgentSession(config, session_id="sess_shutdown", non_interactive=True)

            async def never() -> None:
                await asyncio.sleep(10)

            async def close_all() -> None:
                close_calls.append(True)

            close_calls: list[bool] = []
            state = session.agent_task_store.create(
                agent_id="agent_1",
                agent_type="general",
                description="desc",
                prompt="prompt",
                parent_session_id=session.session_id,
            )
            state.status = "running"
            session.agent_task_store.write_state(state)
            session.mcp_manager.close_all = close_all  # type: ignore[method-assign]

            async def run_shutdown() -> None:
                session._background_subagent_runs["agent_1"] = asyncio.create_task(never())
                await session.shutdown()

            asyncio.run(run_shutdown())

            latest = session.agent_task_store.read_state("agent_1")
            self.assertIsNotNone(latest)
            self.assertEqual(latest.status, "cancelled")
            self.assertEqual(close_calls, [True])

    def test_raw_input_is_confined_to_terminal_helpers(self) -> None:
        allowed = {
            "bigcode/ui/prompt.py",
            "bigcode/tools/runner.py",
            "bigcode/tools/plan/AskUserQuestion.py",
            "bigcode/tools/plan/ExitPlanMode.py",
        }
        offenders: list[str] = []
        root = Path(__file__).resolve().parents[2]
        for path in (root / "bigcode").rglob("*.py"):
            if "__pycache__" in path.parts or "tests" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "input" and rel not in allowed:
                    offenders.append(f"{rel}:{node.lineno}")
        self.assertEqual(offenders, [])


def _make_tool_context(root: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        cwd=root,
        workspace_roots=[root],
        permission_context=ToolPermissionContext(mode="default", should_avoid_permission_prompts=True),
        read_file_state=ReadFileState(),
        abort_event=Event(),
        session_id="sess",
        is_non_interactive_session=True,
    )
