from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from pydantic import BaseModel

from bigcode.agent.session import AgentSession
from bigcode.mcp.client import McpCapability
from bigcode.tools.base import BaseTool, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permissions import ToolPermissionContext
from bigcode.tools.read_file_state import ReadFileState
from bigcode.tools.registry import ToolRegistry, build_default_registry
from bigcode.tools.runner import ToolRunner, ToolUse
from bigcode.tools.mcp import build_mcp_tool_name, register_mcp_tools_from_capabilities
from bigcode.tools.tool_search.Tool_Search import ToolSearchTool


class ToolSearchTests(unittest.TestCase):
    def make_ctx(self, root: Path, *, mode: str = "default") -> ToolExecutionContext:
        return ToolExecutionContext(
            cwd=root,
            workspace_roots=[root.resolve()],
            permission_context=ToolPermissionContext(mode=mode, should_avoid_permission_prompts=True),
            read_file_state=ReadFileState(),
            abort_event=Event(),
            session_id="tool-search-test",
            is_non_interactive_session=True,
        )

    def test_default_registry_keeps_builtin_tools_visible(self) -> None:
        registry = build_default_registry()
        names = {schema["name"] for schema in registry.schemas_for_model()}

        self.assertIn("Tool_Search", names)
        self.assertIn("Read", names)
        self.assertIn("ExternalResourceList", names)

    def test_tool_search_discovers_deferred_tool_by_keyword(self) -> None:
        registry = ToolRegistry()
        registry.register(ToolSearchTool())
        registry.register(VisibleTool())
        registry.register(DeferredTool())

        self.assertEqual(
            {schema["name"] for schema in registry.schemas_for_model()},
            {"Tool_Search", "Visible"},
        )

        with tempfile.TemporaryDirectory() as td:
            result = asyncio.run(
                ToolRunner(registry).run_one(
                    ToolUse("search", "Tool_Search", {"query": "hidden mcp", "max_results": 3}),
                    self.make_ctx(Path(td).resolve()),
                )
            )

        self.assertFalse(result.is_error)
        self.assertEqual(result.output.data["matches"], ["Deferred"])
        self.assertIn("Deferred", result.output.data["discovered"])
        self.assertIn("Deferred", {schema["name"] for schema in registry.schemas_for_model()})

    def test_tool_search_discovers_deferred_tool_by_select(self) -> None:
        registry = ToolRegistry()
        registry.register(ToolSearchTool())
        registry.register(DeferredTool())

        with tempfile.TemporaryDirectory() as td:
            result = asyncio.run(
                ToolRunner(registry).run_one(
                    ToolUse("search", "Tool_Search", {"query": "select:DeferredAlias"}),
                    self.make_ctx(Path(td).resolve()),
                )
            )

        self.assertFalse(result.is_error)
        self.assertEqual(result.output.data["mode"], "select")
        self.assertEqual(result.output.data["matches"], ["Deferred"])

    def test_runner_uses_read_only_for_parallelism(self) -> None:
        registry = ToolRegistry()
        registry.register(SerialReadCategoryTool())

        with tempfile.TemporaryDirectory() as td:
            results = asyncio.run(
                ToolRunner(registry).run_tool_uses(
                    [
                        ToolUse("1", "SerialReadCategory", {"value": 1}),
                        ToolUse("2", "SerialReadCategory", {"value": 2}),
                    ],
                    self.make_ctx(Path(td).resolve()),
                )
            )

        self.assertEqual([result.tool_use_id for result in results], ["1", "2"])
        self.assertEqual(SerialReadCategoryTool.max_active, 1)

    def test_mcp_tool_is_hidden_until_tool_search_discovers_it(self) -> None:
        registry = ToolRegistry()
        registry.register(ToolSearchTool())
        name = build_mcp_tool_name("github", "list_issues")
        added = register_mcp_tools_from_capabilities(
            registry,
            [
                McpCapability(
                    kind="tool",
                    server="github",
                    name="list_issues",
                    description="List GitHub issues for a repository.",
                    schema={"type": "object", "properties": {"repo": {"type": "string"}}},
                    read_only_hint=True,
                    search_hint="github issues repository",
                )
            ],
        )

        self.assertEqual(added, [name])
        self.assertIn(name, {tool.name for tool in registry.list_tools()})
        self.assertNotIn(name, {schema["name"] for schema in registry.schemas_for_model()})

        with tempfile.TemporaryDirectory() as td:
            result = asyncio.run(
                ToolRunner(registry).run_one(
                    ToolUse("search", "Tool_Search", {"query": "github issues"}),
                    self.make_ctx(Path(td).resolve()),
                )
            )

        self.assertFalse(result.is_error)
        self.assertEqual(result.output.data["matches"], [name])
        schemas = {schema["name"]: schema for schema in registry.schemas_for_model()}
        self.assertIn(name, schemas)
        self.assertIn("repo", schemas[name]["input_schema"]["properties"])

    def test_mcp_tool_always_load_is_visible_without_search(self) -> None:
        registry = ToolRegistry()
        registry.register(ToolSearchTool())
        name = build_mcp_tool_name("linear", "viewer")
        register_mcp_tools_from_capabilities(
            registry,
            [
                McpCapability(
                    kind="tool",
                    server="linear",
                    name="viewer",
                    description="Read the current Linear viewer.",
                    always_load=True,
                    read_only_hint=True,
                )
            ],
        )

        self.assertIn(name, {schema["name"] for schema in registry.schemas_for_model()})
        self.assertNotIn(name, {tool.name for tool in registry.deferred_tools()})

    def test_mcp_tool_call_forwards_to_mcp_manager(self) -> None:
        registry = ToolRegistry()
        name = build_mcp_tool_name("github", "create_issue")
        register_mcp_tools_from_capabilities(
            registry,
            [
                McpCapability(
                    kind="tool",
                    server="github",
                    name="create_issue",
                    schema={"type": "object", "properties": {"title": {"type": "string"}}},
                )
            ],
        )
        manager = FakeMcpManager()

        with tempfile.TemporaryDirectory() as td:
            ctx = self.make_ctx(Path(td).resolve(), mode="bypassPermissions")
            ctx.mcp_manager = manager
            result = asyncio.run(
                ToolRunner(registry).run_one(
                    ToolUse("mcp", name, {"title": "Bug"}),
                    ctx,
                )
            )

        self.assertFalse(result.is_error)
        self.assertEqual(manager.calls, [("github", "create_issue", {"title": "Bug"})])
        self.assertEqual(result.output.data["ok"], True)

    def test_session_sync_registers_discovered_mcp_tools(self) -> None:
        registry = ToolRegistry()
        manager = FakeDiscoveryManager(
            [
                McpCapability(
                    kind="tool",
                    server="slack",
                    name="send_message",
                    description="Send a Slack message.",
                )
            ]
        )
        fake_session = SimpleNamespace(
            config=SimpleNamespace(mcp_enabled=True),
            mcp_manager=manager,
            registry=registry,
        )

        added = asyncio.run(AgentSession._sync_mcp_tools_to_registry(fake_session))

        self.assertEqual(added, [build_mcp_tool_name("slack", "send_message")])
        self.assertEqual(manager.discover_calls, 1)


class SimpleInput(BaseModel):
    value: int = 0


class VisibleTool(BaseTool[SimpleInput, dict]):
    name = "Visible"
    description = "Visible test tool."
    input_model = SimpleInput
    permission_category = "read"
    state_effect = "none"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return True

    def is_concurrency_safe(self, input: SimpleInput, ctx: ToolExecutionContext) -> bool:
        return self.is_read_only(input, ctx)

    def is_read_only(self, input: SimpleInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: SimpleInput, ctx: ToolExecutionContext) -> ValidationResult:
        return ValidationResult(True)

    async def call(self, input: SimpleInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        return ToolResult({"value": input.value})


class DeferredTool(VisibleTool):
    name = "Deferred"
    aliases = ("DeferredAlias",)
    description = "Hidden MCP capability for testing deferred discovery."
    should_defer = True
    search_hint = "hidden mcp capability"


class SerialReadCategoryTool(VisibleTool):
    name = "SerialReadCategory"
    description = "Read category tool that is intentionally not read-only."
    active = 0
    max_active = 0

    def is_read_only(self, input: SimpleInput, ctx: ToolExecutionContext) -> bool:
        return False

    async def call(self, input: SimpleInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        SerialReadCategoryTool.active += 1
        SerialReadCategoryTool.max_active = max(SerialReadCategoryTool.max_active, SerialReadCategoryTool.active)
        await asyncio.sleep(0.01)
        SerialReadCategoryTool.active -= 1
        return ToolResult({"value": input.value})


class FakeMcpManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> dict:
        self.calls.append((server_name, tool_name, arguments))
        return {"ok": True, "server": server_name, "tool": tool_name, "arguments": arguments}


class FakeDiscoveryManager:
    fastmcp_available = True

    def __init__(self, capabilities: list[McpCapability]) -> None:
        self.capabilities = capabilities
        self.discover_calls = 0

    async def discover(self):
        self.discover_calls += 1
        return self.capabilities
