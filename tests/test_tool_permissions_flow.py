from __future__ import annotations

import asyncio
import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from threading import Event

from pydantic import ValidationError

from bigcode.hooks.bus import HookHandler
from bigcode.hooks.models import HookInput, HookOutput
from bigcode.hooks import HookBus
from bigcode.tools.plan.AskUserQuestion import AskUserQuestionInput, AskUserQuestionTool
from bigcode.tools.base import BaseTool, ToolExecutionContext, ToolResult
from bigcode.tools.bash.Bash import BashInput, BashTool
from bigcode.tools.permissions import PermissionRule, ToolPermissionContext, decide_permission
from bigcode.tools.read_file_state import ReadFileState
from bigcode.tools.registry import build_default_registry
from bigcode.tools.runner import ToolRunner, ToolUse
from bigcode.tools.write.Write import WriteInput, WriteTool


class FixedPermissionInput(WriteInput):
    pass


class FixedPermissionTool(BaseTool[FixedPermissionInput, dict]):
    name = "FixedPermission"
    input_model = FixedPermissionInput
    permission_category = "write"
    state_effect = "workspace_write"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return True

    def is_concurrency_safe(self, input: FixedPermissionInput, ctx: ToolExecutionContext) -> bool:
        return False

    def is_read_only(self, input: FixedPermissionInput, ctx: ToolExecutionContext) -> bool:
        return False

    async def validate_input(self, input: FixedPermissionInput, ctx: ToolExecutionContext):
        from bigcode.tools.base import ValidationResult

        return ValidationResult(True)

    async def check_permissions(self, input: FixedPermissionInput, ctx: ToolExecutionContext):
        from bigcode.tools.base import PermissionDecision

        return PermissionDecision(input.content, message=f"fixed {input.content}", updated_input=input, decision_reason={"type": "requiresUserInteraction"} if input.content == "ask" else {})

    async def call(self, input: FixedPermissionInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        return ToolResult({"ok": True})


class ApprovePermissionHook(HookHandler):
    name = "approve-permission"
    events = ("PermissionRequest",)

    async def run(self, input: HookInput) -> HookOutput:
        return HookOutput(decision="approve", reason="approved by test hook")


class ToolPermissionFlowTests(unittest.TestCase):
    def make_ctx(
        self,
        root: Path,
        *,
        mode: str = "default",
        avoid_prompts: bool = True,
        hook_bus: HookBus | None = None,
    ) -> ToolExecutionContext:
        return ToolExecutionContext(
            cwd=root,
            workspace_roots=[root.resolve()],
            permission_context=ToolPermissionContext(mode=mode, should_avoid_permission_prompts=avoid_prompts),
            read_file_state=ReadFileState(),
            abort_event=Event(),
            session_id="permission-flow-test",
            hook_bus=hook_bus,
            is_non_interactive_session=True,
        )

    def test_whole_tool_deny_beats_bypass_and_tool_checks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root, mode="bypassPermissions")
            ctx.permission_context.always_deny.append(PermissionRule("Bash", "deny", source="test"))

            decision = asyncio.run(decide_permission(BashTool(), BashInput(command="pwd"), ctx))

            self.assertEqual(decision.behavior, "deny")
            self.assertEqual(decision.reason_type, "rule")

    def test_whole_tool_ask_skips_content_deny_and_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root, mode="bypassPermissions")
            ctx.permission_context.always_ask.append(PermissionRule("Bash", "ask", source="test"))
            ctx.permission_context.always_deny.append(PermissionRule("Bash", "deny", pattern="sudo *", source="test"))

            decision = asyncio.run(decide_permission(BashTool(), BashInput(command="sudo true"), ctx))

            self.assertEqual(decision.behavior, "ask")
            self.assertEqual(decision.reason_type, "rule")

    def test_content_deny_beats_bypass_and_whole_tool_allow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root, mode="bypassPermissions")
            ctx.permission_context.always_allow.append(PermissionRule("Bash", "allow", source="test"))
            ctx.permission_context.always_deny.append(PermissionRule("Bash", "deny", pattern="npm publish*", source="test"))

            decision = asyncio.run(decide_permission(BashTool(), BashInput(command="npm publish --tag latest"), ctx))

            self.assertEqual(decision.behavior, "deny")
            self.assertEqual(decision.reason_type, "rule")

    def test_safety_ask_beats_bypass_and_content_allow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root, mode="bypassPermissions")
            ctx.permission_context.always_allow.append(PermissionRule("Bash", "allow", pattern="cat * | head", source="test"))

            decision = asyncio.run(decide_permission(BashTool(), BashInput(command="cat a.txt | head"), ctx))

            self.assertEqual(decision.behavior, "ask")
            self.assertEqual(decision.reason_type, "safetyCheck")

    def test_passthrough_becomes_ordinary_ask_without_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ctx = self.make_ctx(root, mode="default")

            decision = asyncio.run(decide_permission(WriteTool(), WriteInput(file_path="x.txt", content="x"), ctx))

            self.assertEqual(decision.behavior, "ask")
            self.assertEqual(decision.reason_type, "ordinary")

    def test_generic_defaults_only_apply_to_passthrough(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            allow_ctx = self.make_ctx(root, mode="plan")
            deny_ctx = self.make_ctx(root, mode="acceptEdits")
            ask_ctx = self.make_ctx(root, mode="bypassPermissions")

            allowed = asyncio.run(decide_permission(FixedPermissionTool(), FixedPermissionInput(file_path="x.txt", content="allow"), allow_ctx))
            denied = asyncio.run(decide_permission(FixedPermissionTool(), FixedPermissionInput(file_path="x.txt", content="deny"), deny_ctx))
            asked = asyncio.run(decide_permission(FixedPermissionTool(), FixedPermissionInput(file_path="x.txt", content="ask"), ask_ctx))

            self.assertEqual(allowed.behavior, "allow")
            self.assertEqual(allowed.message, "fixed allow")
            self.assertEqual(denied.behavior, "deny")
            self.assertEqual(denied.message, "fixed deny")
            self.assertEqual(asked.behavior, "ask")
            self.assertEqual(asked.reason_type, "requiresUserInteraction")

    def test_noninteractive_permission_request_hook_can_approve(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            bus = HookBus()
            bus.register(ApprovePermissionHook())
            ctx = self.make_ctx(root, mode="default", avoid_prompts=False, hook_bus=bus)

            result = asyncio.run(ToolRunner(build_default_registry()).run_one(ToolUse("1", "Write", {"file_path": "x.txt", "content": "x"}), ctx))

            self.assertFalse(result.is_error)
            self.assertEqual((root / "x.txt").read_text(encoding="utf-8"), "x")

    def test_default_registry_tools_override_lifecycle_methods(self) -> None:
        methods = {"is_enabled", "is_concurrency_safe", "is_read_only", "validate_input", "check_permissions", "call"}
        missing: list[str] = []
        for tool in build_default_registry().list_tools():
            for method in methods:
                if method not in tool.__class__.__dict__:
                    missing.append(f"{tool.name}.{method}")
        self.assertEqual(missing, [])

    def test_each_tool_has_its_own_tool_named_file(self) -> None:
        seen_modules: set[Path] = set()
        tools_root = Path(__file__).resolve().parents[1] / "bigcode" / "tools"
        for tool in build_default_registry().list_tools():
            module = inspect.getmodule(tool.__class__)
            self.assertIsNotNone(module)
            module_path = Path(module.__file__).resolve()
            self.assertTrue(module_path.is_relative_to(tools_root))
            self.assertEqual(module_path.stem, tool.name)
            self.assertNotIn(module_path, seen_modules)
            seen_modules.add(module_path)

            tree = ast.parse(module_path.read_text(encoding="utf-8"))
            tool_classes = []
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                for base in node.bases:
                    if isinstance(base, ast.Name) and base.id == "BaseTool":
                        tool_classes.append(node.name)
                    elif isinstance(base, ast.Subscript) and isinstance(base.value, ast.Name) and base.value.id == "BaseTool":
                        tool_classes.append(node.name)
            self.assertEqual(tool_classes, [tool.__class__.__name__])

    def test_ask_user_question_accepts_one_to_three_batch_questions(self) -> None:
        valid = AskUserQuestionInput(
            questions=[
                {
                    "question": "Pick one",
                    "kind": "single",
                    "options": [{"label": "A", "description": "Recommended"}, {"label": "B"}],
                },
                {
                    "question": "Pick many",
                    "kind": "multiple",
                    "options": [{"label": "X"}, {"label": "Y"}],
                },
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            ctx = self.make_ctx(Path(td).resolve())
            out = asyncio.run(AskUserQuestionTool().call(valid, ctx))

        self.assertTrue(out.data["requires_answer"])
        self.assertEqual(len(out.data["questions"]), 2)

        with self.assertRaises(ValidationError):
            AskUserQuestionInput(
                questions=[
                    {"question": "1", "options": [{"label": "A"}]},
                    {"question": "2", "options": [{"label": "A"}]},
                    {"question": "3", "options": [{"label": "A"}]},
                    {"question": "4", "options": [{"label": "A"}]},
                ]
            )


if __name__ == "__main__":
    unittest.main()
