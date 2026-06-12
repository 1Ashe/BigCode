from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from pydantic import BaseModel

from bigcode.context.normalizer import tool_run_result_to_message
from bigcode.tools.artifacts.ArtifactRead import ArtifactReadInput, ArtifactReadTool, ArtifactStore
from bigcode.tools.base import BaseTool, ToolExecutionContext, ToolResult
from bigcode.tools.permissions import ToolPermissionContext
from bigcode.tools.read_file_state import ReadFileState
from bigcode.tools.registry import ToolRegistry
from bigcode.tools.runner import ToolRunner, ToolUse


class ArtifactTests(unittest.TestCase):
    def make_ctx(self, root: Path, *, session_id: str = "sess_art", active_artifacts: dict | None = None) -> ToolExecutionContext:
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
            active_artifacts=active_artifacts if active_artifacts is not None else {},
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
            self.assertEqual(result.metadata["artifact_id"], "toolu_big")
            self.assertTrue(Path(result.metadata["artifact_path"]).exists())
            self.assertEqual(ctx.active_artifacts["toolu_big"]["original_chars"], result.metadata["original_chars"])
            self.assertTrue(result.output.metadata["truncated"])
            self.assertTrue(result.output.data["__truncated__"])

            msg = tool_run_result_to_message(result)
            content = msg.content[0].content
            self.assertEqual(content["artifact_id"], "toolu_big")
            self.assertIn("artifact_path", content)
            self.assertIn("original_chars", content)

    def test_artifact_read_current_session_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx = self.make_ctx(root, session_id="sess_a")
            record = ctx.artifact_store.write_tool_output(
                artifact_id="toolu_1",
                tool_use_id="toolu_1",
                tool_name="Read",
                output={"text": "full output"},
            )
            ctx.active_artifacts[record.artifact_id] = {
                "artifact_id": record.artifact_id,
                "artifact_path": record.artifact_path,
                "original_chars": record.original_chars,
            }

            out = asyncio.run(ArtifactReadTool().call(ArtifactReadInput(artifact_id="toolu_1"), ctx))
            self.assertIn("full output", out.data["content"])
            self.assertFalse(out.data["truncated"])

            with self.assertRaisesRegex(RuntimeError, "Unknown artifact"):
                asyncio.run(ArtifactReadTool().call(ArtifactReadInput(artifact_id="missing"), ctx))
            with self.assertRaisesRegex(RuntimeError, "Invalid artifact id"):
                asyncio.run(ArtifactReadTool().call(ArtifactReadInput(artifact_id="../escape"), ctx))

            other_ctx = self.make_ctx(root, session_id="sess_b", active_artifacts=dict(ctx.active_artifacts))
            with self.assertRaisesRegex(RuntimeError, "current session"):
                asyncio.run(ArtifactReadTool().call(ArtifactReadInput(artifact_id="toolu_1"), other_ctx))


if __name__ == "__main__":
    unittest.main()
