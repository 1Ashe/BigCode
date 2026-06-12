from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from pydantic import BaseModel

from bigcode.context.normalizer import tool_run_result_to_message
from bigcode.tools.artifacts import ArtifactStore
from bigcode.tools.base import BaseTool, ToolExecutionContext, ToolResult
from bigcode.tools.permissions import ToolPermissionContext
from bigcode.tools.read.Read import ReadInput, ReadTool
from bigcode.tools.read_file_state import ReadFileState
from bigcode.tools.registry import ToolRegistry
from bigcode.tools.runner import ToolRunner, ToolUse


class ArtifactTests(unittest.TestCase):
    def make_ctx(self, root: Path, *, session_id: str = "sess_art") -> ToolExecutionContext:
        project_state_dir = root / ".state"
        return ToolExecutionContext(
            cwd=root,
            workspace_roots=[root],
            permission_context=ToolPermissionContext(mode="default", should_avoid_permission_prompts=True),
            read_file_state=ReadFileState(),
            abort_event=Event(),
            session_id=session_id,
            is_non_interactive_session=True,
            artifact_store=ArtifactStore(project_state_dir, session_id),
        )

    def test_large_tool_output_is_offloaded_and_metadata_reaches_tool_result(self) -> None:
        class BigInput(BaseModel):
            pass

        class BigTool(BaseTool[BigInput, dict]):
            name = "Big"
            description = "Big output."
            input_model = BigInput
            permission_category = "read"
            state_effect = "none"
            max_result_chars = 100

            async def call(self, input: BigInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
                return ToolResult({"text": "x" * 1000})

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx = self.make_ctx(root)
            registry = ToolRegistry()
            registry.register(BigTool())
            result = asyncio.run(ToolRunner(registry).run_one(ToolUse("toolu_big", "Big", {}), ctx))

            self.assertFalse(result.is_error)
            self.assertTrue(Path(result.metadata["artifact_path"]).exists())
            self.assertNotIn("artifact_id", result.metadata)
            self.assertTrue(result.output.metadata["truncated"])
            self.assertTrue(result.output.data["__truncated__"])

            msg = tool_run_result_to_message(result)
            content = msg.content[0].content
            self.assertIn("artifact_path", content)
            self.assertIn("original_chars", content)
            self.assertNotIn("artifact_id", content)

    def test_large_tool_output_artifact_can_be_read_by_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx = self.make_ctx(root, session_id="sess_a")
            record = ctx.artifact_store.write_tool_output(
                tool_use_id="toolu_1",
                tool_name="Read",
                output={"text": "full output"},
            )

            read_ctx = self.make_ctx(root, session_id="sess_a")
            read_ctx.workspace_roots.append((root / ".state").resolve(strict=False))
            out = asyncio.run(ReadTool().call(ReadInput(file_path=record.artifact_path), read_ctx))
            self.assertIn("full output", out.data["content"])


if __name__ == "__main__":
    unittest.main()
