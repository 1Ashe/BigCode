from __future__ import annotations

import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bigcode.agent.session import AgentSession
from bigcode.config import load_runtime_config
from bigcode.context.messages import AssistantMessage, TextBlock
from bigcode.hooks.bus import HookHandler
from bigcode.hooks.models import HookInput, HookOutput
from bigcode.models.claude_compatible import ModelResponse
from bigcode.tools.subagents.Agent import AgentTool, AgentToolInput
from bigcode.tools.subagents.TaskOutput import TaskOutputInput, TaskOutputTool
from bigcode.tools.subagents.TaskStop import TaskStopInput, TaskStopTool
from bigcode.tools.permissions import ToolPermissionContext
from bigcode.tools.registry import build_default_registry
from bigcode.tools.runner import ToolRunner, ToolUse


class BackgroundSubagentTests(unittest.TestCase):
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

    def test_background_agent_completes_and_task_output_reads_output(self) -> None:
        class FakeClient:
            def __init__(self, model) -> None:
                self.model = model

            async def complete(self, system_prompt, messages, tools):
                text = _api_text(messages)
                label = "one" if "one" in text else "two"
                return ModelResponse(
                    AssistantMessage([TextBlock(text=f"done {label}")], model=self.model.ref, stop_reason="end_turn", usage={"input_tokens": 2, "output_tokens": 3}),
                    raw={},
                )

        async def scenario(config) -> tuple[AgentSession, list[str]]:
            session = AgentSession(config, session_id="sess_bg", non_interactive=True)
            out1 = await AgentTool().call(AgentToolInput(prompt="task one", background=True), session.make_tool_context())
            out2 = await AgentTool().call(AgentToolInput(prompt="task two", run_in_background=True), session.make_tool_context())
            agent_ids = [out1.data["agent_id"], out2.data["agent_id"]]
            self.assertEqual(out1.data["status"], "async_launched")
            self.assertEqual(Path(out1.data["output_file"]).parent.name, "agent-task-outputs")
            await asyncio.gather(*(session._background_subagent_runs[agent_id] for agent_id in agent_ids))
            return session, agent_ids

        with tempfile.TemporaryDirectory() as td, patch("bigcode.agent.session.ClaudeCompatibleModelClient", FakeClient):
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            config = self.make_config(root, home)
            session, agent_ids = asyncio.run(scenario(config))

            states = [session.agent_task_store.read_state(agent_id) for agent_id in agent_ids]
            self.assertEqual([state.status for state in states], ["completed", "completed"])
            self.assertEqual([state.total_tool_use_count for state in states], [0, 0])
            self.assertEqual([state.total_tokens for state in states], [5, 5])
            self.assertNotEqual(states[0].sidechain_transcript_path, states[1].sidechain_transcript_path)
            for state in states:
                self.assertTrue(Path(state.output_file).exists())
                self.assertTrue(Path(state.sidechain_transcript_path).exists())

            ctx = session.make_tool_context()
            listed = asyncio.run(TaskOutputTool().call(TaskOutputInput(), ctx))
            self.assertEqual(listed.data["count"], 2)
            one = asyncio.run(TaskOutputTool().call(TaskOutputInput(agent_id=agent_ids[0], max_chars=12), ctx))
            self.assertIn("done one", one.data["output"])
            self.assertTrue(one.data["truncated"])

    def test_background_agent_failure_and_task_stop_not_running(self) -> None:
        class FailingClient:
            def __init__(self, model) -> None:
                self.model = model

            async def complete(self, system_prompt, messages, tools):
                raise RuntimeError("model exploded")

        async def scenario(config) -> tuple[AgentSession, str]:
            session = AgentSession(config, session_id="sess_fail", non_interactive=True)
            out = await AgentTool().call(AgentToolInput(prompt="fail", background=True), session.make_tool_context())
            agent_id = out.data["agent_id"]
            await session._background_subagent_runs[agent_id]
            stopped = await TaskStopTool().call(TaskStopInput(agent_id=agent_id), session.make_tool_context())
            self.assertEqual(stopped.data["status"], "not_running")
            return session, agent_id

        with tempfile.TemporaryDirectory() as td, patch("bigcode.agent.session.ClaudeCompatibleModelClient", FailingClient):
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            config = self.make_config(root, home)
            session, agent_id = asyncio.run(scenario(config))
            state = session.agent_task_store.read_state(agent_id)
            self.assertEqual(state.status, "failed")
            self.assertIn("model exploded", state.error)
            output, _ = session.agent_task_store.read_output(agent_id, max_chars=1000)
            self.assertIn("[Subagent failed]", output)
            self.assertIn("model exploded", output)

    def test_task_stop_cancels_running_background_agent(self) -> None:
        class SlowClient:
            def __init__(self, model) -> None:
                self.model = model

            async def complete(self, system_prompt, messages, tools):
                await asyncio.sleep(30)
                return ModelResponse(AssistantMessage([TextBlock(text="too late")], model=self.model.ref), raw={})

        async def scenario(config) -> tuple[AgentSession, str, dict]:
            session = AgentSession(config, session_id="sess_cancel", non_interactive=True)
            out = await AgentTool().call(AgentToolInput(prompt="wait", background=True), session.make_tool_context())
            agent_id = out.data["agent_id"]
            for _ in range(50):
                state = session.agent_task_store.read_state(agent_id)
                if state and state.status == "running":
                    break
                await asyncio.sleep(0.01)
            stopped = await TaskStopTool().call(TaskStopInput(agent_id=agent_id), session.make_tool_context())
            return session, agent_id, stopped.data

        with tempfile.TemporaryDirectory() as td, patch("bigcode.agent.session.ClaudeCompatibleModelClient", SlowClient):
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            config = self.make_config(root, home)
            session, agent_id, stopped = asyncio.run(scenario(config))
            self.assertIn(stopped["status"], {"cancelled", "cancelling"})
            state = session.agent_task_store.read_state(agent_id)
            self.assertEqual(state.status, "cancelled")
            output, _ = session.agent_task_store.read_output(agent_id, max_chars=1000)
            self.assertIn("[Subagent cancelled]", output)

    def test_task_stop_cancels_queued_background_agent(self) -> None:
        class SlowClient:
            def __init__(self, model) -> None:
                self.model = model

            async def complete(self, system_prompt, messages, tools):
                await asyncio.sleep(30)
                return ModelResponse(AssistantMessage([TextBlock(text="too late")], model=self.model.ref), raw={})

        async def scenario(config) -> tuple[AgentSession, str, dict]:
            session = AgentSession(config, session_id="sess_cancel_queued", non_interactive=True)
            out = await AgentTool().call(AgentToolInput(prompt="wait", background=True), session.make_tool_context())
            agent_id = out.data["agent_id"]
            stopped = await TaskStopTool().call(TaskStopInput(agent_id=agent_id), session.make_tool_context())
            return session, agent_id, stopped.data

        with tempfile.TemporaryDirectory() as td, patch("bigcode.agent.session.ClaudeCompatibleModelClient", SlowClient):
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            config = self.make_config(root, home)
            session, agent_id, stopped = asyncio.run(scenario(config))
            self.assertEqual(stopped["status"], "cancelled")
            state = session.agent_task_store.read_state(agent_id)
            self.assertEqual(state.status, "cancelled")
            output, _ = session.agent_task_store.read_output(agent_id, max_chars=1000)
            self.assertIn("[Subagent cancelled]", output)

    def test_sync_subagent_emits_hooks_and_uses_sidechain_transcript(self) -> None:
        class FakeClient:
            def __init__(self, model) -> None:
                self.model = model

            async def complete(self, system_prompt, messages, tools):
                return ModelResponse(AssistantMessage([TextBlock(text="sync done")], model=self.model.ref, stop_reason="end_turn"), raw={})

        class CaptureHook(HookHandler):
            name = "CaptureHook"
            events = ("SubagentStart", "SubagentStop")

            def __init__(self) -> None:
                self.inputs: list[HookInput] = []

            async def run(self, input: HookInput) -> HookOutput:
                self.inputs.append(input)
                return HookOutput()

        async def scenario(config) -> tuple[AgentSession, CaptureHook, dict]:
            session = AgentSession(config, session_id="sess_sync", non_interactive=True)
            hook = CaptureHook()
            session.hook_bus.register(hook)
            out = await AgentTool().call(AgentToolInput(prompt="sync task"), session.make_tool_context())
            return session, hook, out.data

        with tempfile.TemporaryDirectory() as td, patch("bigcode.agent.session.ClaudeCompatibleModelClient", FakeClient):
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            config = self.make_config(root, home)
            session, hook, out = asyncio.run(scenario(config))
            self.assertEqual(out["status"], "completed")
            self.assertEqual(out["content"], "sync done")
            self.assertEqual([item.hook_event_name for item in hook.inputs], ["SubagentStart", "SubagentStop"])
            sidechain = Path(hook.inputs[0].payload["sidechain_path"])
            self.assertTrue(sidechain.exists())
            self.assertGreaterEqual(len(sidechain.read_text(encoding="utf-8").splitlines()), 2)
            self.assertEqual(session.messages, [])

    def test_task_output_validation_and_plan_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            config = self.make_config(root, home)
            session = AgentSession(config, session_id="sess_policy", non_interactive=True)
            session.permission_context = ToolPermissionContext(mode="plan", should_avoid_permission_prompts=True)
            registry = build_default_registry()
            names = {tool.name for tool in registry.list_tools()}
            self.assertIn("TaskOutput", names)
            self.assertIn("TaskStop", names)

            output_result = asyncio.run(ToolRunner(registry).run_one(ToolUse("1", "TaskOutput", {}), session.make_tool_context()))
            self.assertFalse(output_result.is_error)
            stop_result = asyncio.run(ToolRunner(registry).run_one(ToolUse("2", "TaskStop", {"agent_id": "missing"}), session.make_tool_context()))
            self.assertTrue(stop_result.is_error)
            self.assertIn("Plan Mode", stop_result.error_message)
            with self.assertRaisesRegex(RuntimeError, "Invalid agent_id"):
                asyncio.run(TaskOutputTool().call(TaskOutputInput(agent_id="../escape"), session.make_tool_context()))

    def test_status_prints_background_subagent_counts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            home = Path(td) / "home"
            root.mkdir()
            config = self.make_config(root, home)
            session = AgentSession(config, session_id="sess_status", non_interactive=True)
            session.agent_task_store.create(
                agent_id="subagent_status_1",
                agent_type="general-purpose",
                description="status",
                prompt="status",
                parent_session_id=session.session_id,
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                asyncio.run(session.handle_command("/status"))
            rendered = out.getvalue()
            self.assertIn("sandbox profile: none", rendered)
            self.assertIn("background subagents: 1", rendered)
            self.assertIn("queued 1", rendered)


def _api_text(messages) -> str:
    parts: list[str] = []
    for message in messages:
        for block in message.content:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
    return "\n".join(parts)


if __name__ == "__main__":
    unittest.main()
