from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from threading import Event
from typing import Any

import httpx
from pydantic import BaseModel

from bigcode.config import load_runtime_config
from bigcode.config.models import ResolvedModel
from bigcode.context.attachments import Attachment
from bigcode.context.builder import ContextBuildDeps, build_context_for_api
from bigcode.context.messages import (
    ApiMessage,
    AssistantMessage,
    AttachmentMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from bigcode.context.normalizer import normalize_messages_for_api
from bigcode.hooks import HookBus
from bigcode.hooks.builtins import register_builtin_hooks
from bigcode.mcp import McpClientManager
from bigcode.models import ClaudeCompatibleModelClient
from bigcode.plan import PlanModeState, PlanStore
from bigcode.tools.plan.EnterPlanMode import EnterPlanModeTool
from bigcode.tools.plan.WritePlan import WritePlanInput, WritePlanTool
from bigcode.skills.loader import load_skills
from bigcode.tools.skills.SkillLoad import SkillLoadInput, SkillLoadTool
from bigcode.tools.skills.SkillResourceRead import SkillResourceReadInput, SkillResourceReadTool
from bigcode.subagents.definitions import AgentDefinition
from bigcode.tasks.store import TaskStore
from bigcode.tools.tasks.TaskCreate import TaskCreateInput
from bigcode.tools.tasks.TaskUpdate import TaskUpdateInput
from bigcode.agent.session import _format_exception, _registry_for_subagent
from bigcode.tools.bash.Bash import BashInput
from bigcode.tools.base import BaseTool, ToolExecutionContext, ToolResult
from bigcode.tools.base import EmptyInput
from bigcode.tools.permissions import ToolPermissionContext, classify_bash, decide_permission
from bigcode.tools.read_file_state import ReadFileState
from bigcode.tools.edit.Edit import EditInput, EditTool
from bigcode.tools.read.Read import ReadInput, ReadTool
from bigcode.tools.registry import ToolRegistry, build_default_registry
from bigcode.tools.runner import ToolRunner, ToolUse, _format_permission_prompt
from bigcode.tools.write.Write import WriteInput, WriteTool


class BigCodeCoreTests(unittest.TestCase):
    def make_ctx(self, root: Path, *, mode: str = "default") -> ToolExecutionContext:
        return ToolExecutionContext(
            cwd=root,
            workspace_roots=[root.resolve()],
            permission_context=ToolPermissionContext(mode=mode, should_avoid_permission_prompts=True),
            read_file_state=ReadFileState(),
            abort_event=Event(),
            session_id="test-session",
            is_non_interactive_session=True,
        )

    def test_default_registry_loads(self) -> None:
        registry = build_default_registry()
        names = {tool.name for tool in registry.list_tools()}
        self.assertIn("Read", names)
        self.assertIn("TaskCreate", names)
        self.assertIn("Agent", names)

    def test_config_model_resolution(self) -> None:
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
                      "type": "openai-compatible",
                      "base_url": "http://127.0.0.1:8000/v1",
                      "models": {"test": {"id": "test-model"}}
                    }
                  }
                }
                """,
                encoding="utf-8",
            )
            config = load_runtime_config(root)
            self.assertEqual(config.default_model_ref, "local:test")
            self.assertEqual(config.models["local:test"].model_id, "test-model")
            self.assertEqual(config.models["local:test"].provider_type, "claude-compatible")

    def test_config_allows_dotted_model_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_dir = root / ".bigcode"
            cfg_dir.mkdir()
            (cfg_dir / "models.json").write_text(
                """
                {
                  "default_model": "mimo:MiMo-V2.5-Pro",
                  "providers": {
                    "mimo": {
                      "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
                      "api_key_env": "MIMO_API_KEY",
                      "models": {"MiMo-V2.5-Pro": {"id": "MiMo-V2.5-Pro"}}
                    }
                  }
                }
                """,
                encoding="utf-8",
            )
            config = load_runtime_config(root)
            self.assertIn("mimo:MiMo-V2.5-Pro", config.models)
            self.assertFalse(any("invalid model key" in err for err in config.config_errors))

    def test_config_warns_and_ignores_plaintext_api_key_env(self) -> None:
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
                      "api_key_env": "sk-plaintext-token",
                      "models": {"test": {"id": "test-model"}}
                    }
                  }
                }
                """,
                encoding="utf-8",
            )
            config = load_runtime_config(root)
            self.assertIsNone(config.models["local:test"].api_key_env)
            self.assertTrue(any("plaintext token" in err for err in config.config_errors))

    def test_claude_client_payload_and_response(self) -> None:
        class FakeAsyncClient:
            last: dict[str, Any] = {}

            def __init__(self, *, timeout: int) -> None:
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]):
                FakeAsyncClient.last = {"url": url, "headers": headers, "json": json}
                return httpx.Response(
                    200,
                    json={
                        "content": [
                            {"type": "text", "text": "ok"},
                            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "a.txt"}},
                        ],
                        "stop_reason": "tool_use",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                    request=httpx.Request("POST", url),
                )

        model = ResolvedModel(
            ref="local:test",
            provider="local",
            model_key="test",
            model_id="claude-test",
            base_url="https://api.example.test/v1",
            api_key_env="TEST_API_KEY",
            default_headers={"anthropic-version": "2024-01-01", "x-api-key": "override-key"},
            max_output_tokens=123,
        )
        msg = ApiMessage(role="user", content=[{"type": "text", "text": "hello"}])
        with patch.dict(os.environ, {"TEST_API_KEY": "secret"}), patch("bigcode.models.claude_compatible.httpx.AsyncClient", FakeAsyncClient):
            response = asyncio.run(
                ClaudeCompatibleModelClient(model).complete(
                    "system prompt",
                    [msg],
                    [{"name": "Read", "description": "read", "input_schema": {"type": "object"}}],
                )
            )
        self.assertEqual(FakeAsyncClient.last["url"], "https://api.example.test/v1/messages")
        self.assertEqual(FakeAsyncClient.last["headers"]["x-api-key"], "override-key")
        self.assertEqual(FakeAsyncClient.last["headers"]["anthropic-version"], "2024-01-01")
        self.assertEqual(FakeAsyncClient.last["json"]["system"], "system prompt")
        self.assertEqual(FakeAsyncClient.last["json"]["max_tokens"], 123)
        self.assertIn("tools", FakeAsyncClient.last["json"])
        self.assertEqual(response.message.stop_reason, "tool_use")
        self.assertEqual(response.message.content[1].name, "Read")

    def test_claude_client_reports_missing_api_key(self) -> None:
        model = ResolvedModel(
            ref="mimo:MiMo-V2.5-Pro",
            provider="mimo",
            model_key="MiMo-V2.5-Pro",
            model_id="MiMo-V2.5-Pro",
            base_url="https://token-plan-cn.xiaomimimo.com/v1",
            api_key_env="MIMO_API_KEY",
        )
        msg = ApiMessage(role="user", content=[{"type": "text", "text": "hello"}])
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "MIMO_API_KEY"):
                asyncio.run(ClaudeCompatibleModelClient(model).complete("system", [msg], []))

    def test_claude_client_reports_blank_request_errors(self) -> None:
        class FailingAsyncClient:
            def __init__(self, *, timeout: int) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]):
                raise httpx.ReadError("", request=httpx.Request("POST", url))

        model = ResolvedModel(
            ref="local:test",
            provider="local",
            model_key="test",
            model_id="test",
            base_url="https://api.example.test/v1",
            api_key_env=None,
        )
        msg = ApiMessage(role="user", content=[{"type": "text", "text": "hello"}])
        with patch("bigcode.models.claude_compatible.httpx.AsyncClient", FailingAsyncClient):
            with self.assertRaisesRegex(RuntimeError, "ReadError"):
                asyncio.run(ClaudeCompatibleModelClient(model).complete("system", [msg], []))

    def test_repl_exception_formatter_falls_back_to_class_name(self) -> None:
        self.assertEqual(_format_exception(Exception()), "Exception")

    def test_tool_schema_is_claude_format(self) -> None:
        schema = build_default_registry().schemas_for_model()[0]
        self.assertIn("name", schema)
        self.assertIn("description", schema)
        self.assertIn("input_schema", schema)
        self.assertNotIn("function", schema)

    def test_normalizer_projects_claude_tool_blocks(self) -> None:
        api = normalize_messages_for_api(
            "system",
            [
                AssistantMessage([]),
                AssistantMessage([ToolUseBlock(id="use_1", name="Read", input={"file_path": "a.txt"})]),
                UserMessage([ToolResultBlock(tool_use_id="use_1", content={"type": "text"}, is_error=False)]),
                UserMessage([ToolResultBlock(tool_use_id="orphan", content="lost", is_error=True)]),
            ],
        )
        self.assertEqual([msg.role for msg in api], ["assistant", "user"])
        self.assertEqual(api[0].content[0]["type"], "tool_use")
        self.assertEqual(api[1].content[0]["type"], "tool_result")
        self.assertEqual(api[1].content[1]["type"], "text")
        self.assertIn("Orphaned tool result", api[1].content[1]["text"])

    def test_normalizer_reorders_and_merges_attachments(self) -> None:
        api = normalize_messages_for_api(
            "system",
            [
                UserMessage("implement this"),
                AttachmentMessage(Attachment(type="context", text="extra context", source="hooks")),
            ],
        )

        self.assertEqual(len(api), 1)
        self.assertEqual(api[0].role, "user")
        self.assertIn("extra context", api[0].content[0]["text"])
        self.assertEqual(api[0].content[1]["text"], "implement this")

    def test_normalizer_keeps_attachments_after_tool_results(self) -> None:
        api = normalize_messages_for_api(
            "system",
            [
                AssistantMessage([ToolUseBlock(id="use_1", name="Read", input={})]),
                UserMessage([ToolResultBlock(tool_use_id="use_1", content="result")], is_meta=True, origin="tool"),
                AttachmentMessage(Attachment(type="hook_context", text="post tool context", source="hooks")),
            ],
        )

        self.assertEqual([msg.role for msg in api], ["assistant", "user"])
        self.assertEqual(api[1].content[0]["type"], "tool_result")
        self.assertEqual(api[1].content[1]["type"], "text")
        self.assertIn("post tool context", api[1].content[1]["text"])

    def test_read_duplicate_and_write_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            path = root / "a.txt"
            path.write_text("hello\nworld\n", encoding="utf-8")
            ctx = self.make_ctx(root, mode="acceptEdits")
            read = ReadTool()
            first = asyncio.run(read.call(ReadInput(file_path="a.txt"), ctx))
            self.assertEqual(first.data["type"], "text")
            second = asyncio.run(read.call(ReadInput(file_path="a.txt"), ctx))
            self.assertEqual(second.data["type"], "file_unchanged")
            write = WriteTool()
            asyncio.run(write.call(WriteInput(file_path="a.txt", content="changed\n"), ctx))
            snap = ctx.read_file_state.get_snapshot(path)
            self.assertIsNotNone(snap)
            self.assertFalse(snap.is_partial_view)
            self.assertEqual(snap.content, "changed\n")
            third = asyncio.run(read.call(ReadInput(file_path="a.txt"), ctx))
            self.assertEqual(third.data["type"], "text")
            self.assertIn("changed", third.data["content"])

    def test_read_duplicate_tracks_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            path = root / "a.txt"
            path.write_text("one\ntwo\nthree\n", encoding="utf-8")
            ctx = self.make_ctx(root, mode="acceptEdits")
            read = ReadTool()
            first = asyncio.run(read.call(ReadInput(file_path="a.txt", offset=1, limit=1), ctx))
            self.assertEqual(first.data["type"], "text")
            second = asyncio.run(read.call(ReadInput(file_path="a.txt", offset=1, limit=1), ctx))
            self.assertEqual(second.data["type"], "file_unchanged")
            third = asyncio.run(read.call(ReadInput(file_path="a.txt", offset=0, limit=1), ctx))
            self.assertEqual(third.data["type"], "text")
            path.write_text("one\nchanged\nthree\n", encoding="utf-8")
            fourth = asyncio.run(read.call(ReadInput(file_path="a.txt", offset=1, limit=1), ctx))
            self.assertEqual(fourth.data["type"], "text")
            self.assertIn("changed", fourth.data["content"])

    def test_partial_read_does_not_allow_edit_or_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            path = root / "a.txt"
            path.write_text("one\ntwo\nthree\n", encoding="utf-8")
            ctx = self.make_ctx(root, mode="bypassPermissions")
            asyncio.run(ReadTool().call(ReadInput(file_path="a.txt", offset=0, limit=1), ctx))

            with self.assertRaisesRegex(RuntimeError, "fully read"):
                asyncio.run(EditTool().call(EditInput(file_path="a.txt", old_string="one", new_string="ONE"), ctx))
            with self.assertRaisesRegex(RuntimeError, "fully read"):
                asyncio.run(WriteTool().call(WriteInput(file_path="a.txt", content="changed\n"), ctx))

    def test_existing_file_write_requires_full_read_in_relaxed_modes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            path = root / "a.txt"
            path.write_text("old\n", encoding="utf-8")
            for mode in ("default", "acceptEdits", "bypassPermissions"):
                with self.subTest(mode=mode):
                    ctx = self.make_ctx(root, mode=mode)
                    with self.assertRaisesRegex(RuntimeError, "read before editing"):
                        asyncio.run(WriteTool().call(WriteInput(file_path="a.txt", content="new\n"), ctx))

    def test_new_file_write_records_full_snapshot_without_prior_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root, mode="bypassPermissions")

            asyncio.run(WriteTool().call(WriteInput(file_path="new.txt", content="created\n"), ctx))

            snap = ctx.read_file_state.get_snapshot(root / "new.txt")
            self.assertIsNotNone(snap)
            self.assertEqual(snap.content, "created\n")
            self.assertEqual(snap.source, "write")
            self.assertFalse(snap.is_partial_view)
            self.assertIsNone(snap.offset)
            self.assertIsNone(snap.limit)

    def test_mtime_change_with_same_content_still_allows_edit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            path = root / "a.txt"
            path.write_text("one\n", encoding="utf-8")
            ctx = self.make_ctx(root, mode="bypassPermissions")
            asyncio.run(ReadTool().call(ReadInput(file_path="a.txt"), ctx))

            path.write_text("one\n", encoding="utf-8")
            asyncio.run(EditTool().call(EditInput(file_path="a.txt", old_string="one", new_string="two"), ctx))

            self.assertEqual(path.read_text(encoding="utf-8"), "two\n")

    def test_plan_mode_denies_write_permission(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root, mode="plan")
            decision = asyncio.run(decide_permission(WriteTool(), WriteInput(file_path="x.txt", content="x"), ctx))
            self.assertEqual(decision.behavior, "deny")

    def test_sensitive_file_hard_deny(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            ctx = self.make_ctx(root, mode="bypassPermissions")
            decision = asyncio.run(decide_permission(ReadTool(), ReadInput(file_path=".env"), ctx))
            self.assertEqual(decision.behavior, "deny")
            self.assertIn("sensitive", decision.message)

    def test_tool_runner_noninteractive_ask_denies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            registry = build_default_registry()
            runner = ToolRunner(registry)
            ctx = self.make_ctx(root, mode="default")
            result = asyncio.run(runner.run_one(ToolUse("1", "Write", {"file_path": "x.txt", "content": "x"}), ctx))
            self.assertTrue(result.is_error)
            self.assertIn("permission", result.error_message)

    def test_permission_prompt_summarizes_bash_command_in_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root)
            prompt = _format_permission_prompt(
                "Bash",
                "Shell command is not provably read-only.",
                BashInput(command="python -m bigcode --cwd /home/qt/BigCode", timeout=45),
                ctx,
            )
            self.assertEqual(prompt.count("\n"), 1)
            self.assertIn("需要执行命令：python -m bigcode --cwd /home/qt/BigCode", prompt)
            self.assertIn("是否允许？[y/N]", prompt)

    def test_permission_prompt_summarizes_bash_rm_as_delete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root)
            prompt = _format_permission_prompt("Bash", "Bash requires permission.", BashInput(command="rm /home/qt/tmp/hello.py"), ctx)
            self.assertEqual(prompt.count("\n"), 1)
            self.assertIn("需要删除文件/路径：/home/qt/tmp/hello.py", prompt)
            self.assertNotIn("Tool:", prompt)
            self.assertNotIn("Reason:", prompt)

    def test_permission_prompt_summarizes_compound_bash_without_misreading_control_ops(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root)
            prompt = _format_permission_prompt(
                "Bash",
                "Bash requires permission.",
                BashInput(command="rm /home/qt/tmp/hello.py && ls /home/qt/tmp/"),
                ctx,
            )
            self.assertIn("需要执行 2 条 shell 命令", prompt)
            self.assertIn("删除文件/路径：/home/qt/tmp/hello.py", prompt)
            self.assertIn("查看目录：/home/qt/tmp", prompt)
            self.assertNotIn("/home/qt/BigCode/&&", prompt)
            self.assertNotIn("4 个文件/路径", prompt)

    def test_permission_prompt_keeps_quoted_shell_control_text_inside_argument(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root)
            prompt = _format_permission_prompt("Bash", "Bash requires permission.", BashInput(command='rm "a && b.txt"'), ctx)
            self.assertIn(f"需要删除文件/路径：{root / 'a && b.txt'}", prompt)
            self.assertNotIn("2 条 shell 命令", prompt)

    def test_permission_prompt_uses_complex_summary_for_hard_shell_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ctx = self.make_ctx(Path(td).resolve())
            for command in ["cat a.txt | head", "rm *.py", "echo $(whoami)", "cmd > out.txt"]:
                with self.subTest(command=command):
                    prompt = _format_permission_prompt("Bash", "Bash requires permission.", BashInput(command=command), ctx)
                    self.assertIn("需要执行复杂 shell 命令", prompt)
                    self.assertIn(command, prompt)
                    self.assertNotIn("需要删除文件/路径", prompt)

    def test_permission_prompt_summarizes_common_bash_actions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root)
            cases = [
                ("mkdir tmp", f"需要创建目录：{root / 'tmp'}"),
                ("mv old.txt new.txt", f"需要移动/重命名：{root / 'old.txt'} -> {root / 'new.txt'}"),
                ("cp old.txt new.txt", f"需要复制：{root / 'old.txt'} -> {root / 'new.txt'}"),
                ("git status", "需要查看 Git 状态"),
            ]
            for command, expected in cases:
                with self.subTest(command=command):
                    prompt = _format_permission_prompt("Bash", "Bash requires permission.", BashInput(command=command), ctx)
                    self.assertIn(expected, prompt)

    def test_permission_prompt_summarizes_write_in_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root)
            content = "line1\nline2\nline3\nline4\nline5\nline6\n"
            prompt = _format_permission_prompt("Write", "Write requires permission.", WriteInput(file_path="notes.txt", content=content), ctx)
            self.assertEqual(prompt.count("\n"), 1)
            self.assertIn(f"需要写入文件：{root / 'notes.txt'}", prompt)
            self.assertIn("36 字符，6 行", prompt)
            self.assertNotIn("line1", prompt)

    def test_permission_prompt_redacts_sensitive_generic_input(self) -> None:
        class SecretInput(BaseModel):
            api_key: str
            query: str
            nested: dict[str, str]

        with tempfile.TemporaryDirectory() as td:
            ctx = self.make_ctx(Path(td).resolve())
            prompt = _format_permission_prompt(
                "CustomTool",
                "CustomTool requires permission.",
                SecretInput(api_key="sk-secret", query="hello", nested={"authorization": "Bearer token", "safe": "shown"}),
                ctx,
            )
            self.assertIn('"api_key": "<redacted>"', prompt)
            self.assertIn('"authorization": "<redacted>"', prompt)
            self.assertIn('"safe": "shown"', prompt)
            self.assertNotIn("sk-secret", prompt)
            self.assertNotIn("Bearer token", prompt)
            self.assertEqual(prompt.count("\n"), 1)

    def test_permission_prompt_truncates_long_generic_input(self) -> None:
        class LongInput(BaseModel):
            payload: str

        with tempfile.TemporaryDirectory() as td:
            ctx = self.make_ctx(Path(td).resolve())
            prompt = _format_permission_prompt("CustomTool", "CustomTool requires permission.", LongInput(payload="x" * 1000), ctx)
            self.assertIn("...", prompt)
            self.assertLess(len(prompt), 260)
            self.assertEqual(prompt.count("\n"), 1)

    def test_tool_runner_glob_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "a.md").write_text("a", encoding="utf-8")
            registry = build_default_registry()
            runner = ToolRunner(registry)
            ctx = self.make_ctx(root, mode="default")
            result = asyncio.run(runner.run_one(ToolUse("1", "Glob", {"pattern": "*.md"}), ctx))
            self.assertFalse(result.is_error)
            self.assertEqual(result.output.data["count"], 1)

    def test_tool_runner_parallelizes_state_free_tools_in_order(self) -> None:
        class MarkerInput(BaseModel):
            value: int

        class MarkerTool(BaseTool[MarkerInput, dict]):
            name = "Marker"
            description = "Concurrency marker."
            input_model = MarkerInput
            permission_category = "read"
            state_effect = "none"
            active = 0
            max_active = 0

            async def call(self, input: MarkerInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
                MarkerTool.active += 1
                MarkerTool.max_active = max(MarkerTool.max_active, MarkerTool.active)
                await asyncio.sleep(0.02)
                MarkerTool.active -= 1
                return ToolResult({"value": input.value})

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            registry = ToolRegistry()
            registry.register(MarkerTool())
            ctx = self.make_ctx(root, mode="default")
            started = time.perf_counter()
            results = asyncio.run(
                ToolRunner(registry).run_tool_uses(
                    [ToolUse("1", "Marker", {"value": 1}), ToolUse("2", "Marker", {"value": 2})],
                    ctx,
                )
            )
            elapsed = time.perf_counter() - started
            self.assertEqual([r.tool_use_id for r in results], ["1", "2"])
            self.assertGreater(MarkerTool.max_active, 1)
            self.assertLess(elapsed, 0.04)

    def test_bash_complex_commands_are_not_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root, mode="default")
            self.assertEqual(classify_bash("cat a.txt | head"), "unknown")
            self.assertEqual(classify_bash("sed -i s/a/b/ a.txt"), "mutate")
            result = asyncio.run(ToolRunner(build_default_registry()).run_one(ToolUse("1", "Bash", {"command": "cat a.txt | head"}), ctx))
            self.assertTrue(result.is_error)
            self.assertIn("permission", result.error_message.lower())

    def test_web_fetch_rejects_local_and_redirect_targets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root, mode="bypassPermissions")
            registry = build_default_registry()
            local = asyncio.run(ToolRunner(registry).run_one(ToolUse("1", "WebFetch", {"url": "http://127.0.0.1:8000"}), ctx))
            self.assertTrue(local.is_error)
            self.assertIn("Localhost", local.error_message)
            metadata = asyncio.run(ToolRunner(registry).run_one(ToolUse("2", "WebFetch", {"url": "http://169.254.169.254/latest"}), ctx))
            self.assertTrue(metadata.is_error)

        class RedirectingClient:
            def __init__(self, *, follow_redirects: bool, timeout: int) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, url: str, *, headers: dict[str, str]):
                return httpx.Response(302, headers={"location": "http://localhost/private"}, request=httpx.Request("GET", url))

        with tempfile.TemporaryDirectory() as td, patch("bigcode.tools.web_fetch.WebFetch.httpx.AsyncClient", RedirectingClient):
            root = Path(td).resolve()
            ctx = self.make_ctx(root, mode="bypassPermissions")
            result = asyncio.run(ToolRunner(registry).run_one(ToolUse("3", "WebFetch", {"url": "https://example.com/start"}), ctx))
            self.assertTrue(result.is_error)
            self.assertIn("Localhost", result.error_message)

    def test_plan_write_tool(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root, mode="default")
            ctx.plan_state = PlanModeState()
            ctx.plan_store = PlanStore(root / ".bigcode" / "plans")
            asyncio.run(EnterPlanModeTool().call(EmptyInput(), ctx))
            out = asyncio.run(WritePlanTool().call(WritePlanInput(content="# Plan\nDo it."), ctx))
            self.assertTrue(Path(out.data["path"]).exists())

    def test_plan_mode_and_approved_plan_context_injection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            bus = HookBus()
            register_builtin_hooks(bus)
            active_state = PlanModeState(active=True, plan_file=str(root / "plan.md"))
            built = asyncio.run(
                build_context_for_api(
                    [],
                    ContextBuildDeps(
                        session_id="s1",
                        cwd=root,
                        instruction_paths=[],
                        tool_names=["Read", "WritePlan"],
                        hook_bus=bus,
                        permission_mode="plan",
                        plan_mode_state=active_state,
                    ),
                )
            )
            active_text = built.api_messages[0].content[0]["text"]
            self.assertIsInstance(built.context_messages[0], AttachmentMessage)
            self.assertIn("Write the final implementation plan", active_text)

            cap_built = asyncio.run(
                build_context_for_api(
                    [],
                    ContextBuildDeps(
                        session_id="caps",
                        cwd=root,
                        instruction_paths=[],
                        tool_names=[],
                        hook_bus=bus,
                        capabilities=["Skill zeta", "Skill alpha"],
                    ),
                )
            )
            cap_text = cap_built.api_messages[0].content[0]["text"]
            self.assertIn("Untrusted external capabilities", cap_text)
            self.assertLess(cap_text.index("Skill alpha"), cap_text.index("Skill zeta"))

            exit_state = PlanModeState(approved_plan="# Approved\nDo it.", needs_exit_attachment=True)
            built = asyncio.run(
                build_context_for_api(
                    [],
                    ContextBuildDeps(
                        session_id="s2",
                        cwd=root,
                        instruction_paths=[],
                        tool_names=[],
                        hook_bus=bus,
                        permission_mode="default",
                        plan_mode_state=exit_state,
                    ),
                )
            )
            self.assertIn("# Approved", built.api_messages[0].content[0]["text"])
            self.assertFalse(exit_state.needs_exit_attachment)
            second = asyncio.run(
                build_context_for_api(
                    [],
                    ContextBuildDeps(
                        session_id="s2",
                        cwd=root,
                        instruction_paths=[],
                        tool_names=[],
                        hook_bus=bus,
                        permission_mode="default",
                        plan_mode_state=exit_state,
                    ),
                )
            )
            self.assertEqual(second.api_messages, [])

    def test_task_store_create_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(Path(td))
            task_id = store.create("list", TaskCreateInput(subject="Do thing", description="Details"))
            self.assertEqual(task_id, "1")
            self.assertEqual(store.list("list")[0].subject, "Do thing")

    def test_task_claim_and_block_task(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(Path(td))
            first = store.create("list", TaskCreateInput(subject="First", description="A"))
            second = store.create("list", TaskCreateInput(subject="Second", description="B"))
            source, target = store.block_task("list", first, second)
            self.assertEqual(source.blocks, [second])
            self.assertEqual(target.blocked_by, [first])
            blocked = store.claim("list", second, "worker")
            self.assertFalse(blocked.claimed)
            self.assertIn("blocked", blocked.reason)
            claimed_first = store.claim("list", first, "worker")
            self.assertTrue(claimed_first.claimed)
            store.update("list", first, TaskUpdateInput(id=first, status="completed"))
            claimed_second = store.claim("list", second, "worker", check_busy=True)
            self.assertTrue(claimed_second.claimed)

    def test_skill_load(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo\ndescription: Demo skill\n---\nUse me.", encoding="utf-8")
            registry = load_skills([root / "skills"])
            ctx = self.make_ctx(root)
            ctx.skill_registry = registry
            out = asyncio.run(SkillLoadTool().call(SkillLoadInput(name="demo"), ctx))
            self.assertIn("Use me", out.data["content"])

    def test_skill_load_truncates_and_suggests_similar_names(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "skills" / "demo-skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo-skill\n---\n" + "x" * 200, encoding="utf-8")
            ctx = self.make_ctx(root)
            ctx.skill_registry = load_skills([root / "skills"])
            out = asyncio.run(SkillLoadTool().call(SkillLoadInput(name="demo-skill", max_chars=50), ctx))
            self.assertTrue(out.data["truncated"])
            self.assertLess(len(out.data["content"]), 80)
            with self.assertRaisesRegex(RuntimeError, "Did you mean"):
                asyncio.run(SkillLoadTool().call(SkillLoadInput(name="demo-skll"), ctx))

    def test_skill_resource_read_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo\n---\nUse me.", encoding="utf-8")
            (root / "secret.txt").write_text("secret", encoding="utf-8")
            ctx = self.make_ctx(root)
            ctx.skill_registry = load_skills([root / "skills"])
            with self.assertRaisesRegex(RuntimeError, "Invalid skill resource path"):
                asyncio.run(SkillResourceReadTool().call(SkillResourceReadInput(name="demo", resource_path="../secret.txt"), ctx))

    def test_skill_load_allowed_through_runner_in_default_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            skill = root / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo\n---\nUse me.", encoding="utf-8")
            registry = build_default_registry()
            ctx = self.make_ctx(root, mode="default")
            ctx.skill_registry = load_skills([root / "skills"])
            result = asyncio.run(ToolRunner(registry).run_one(ToolUse("1", "SkillLoad", {"name": "demo"}), ctx))
            self.assertFalse(result.is_error)

    def test_read_only_subagent_tool_pool_is_narrowed(self) -> None:
        registry = build_default_registry()
        definition = AgentDefinition(
            name="readonly",
            description="readonly",
            system_prompt="read only",
            tools=None,
            permission_mode="plan",
        )
        child = _registry_for_subagent(registry, definition)
        names = {tool.name for tool in child.list_tools()}
        self.assertIn("Read", names)
        self.assertIn("Bash", names)
        self.assertNotIn("Agent", names)
        self.assertNotIn("Write", names)
        self.assertNotIn("Edit", names)
        self.assertNotIn("WritePlan", names)
        self.assertNotIn("ExitPlanMode", names)
        self.assertNotIn("TaskCreate", names)
        self.assertNotIn("TaskClaim", names)

    def test_mcp_missing_dependency(self) -> None:
        manager = McpClientManager({}, enabled=True)
        if not manager.fastmcp_available:
            with self.assertRaisesRegex(RuntimeError, "FastMCP"):
                asyncio.run(manager.list_resources())


if __name__ == "__main__":
    unittest.main()
