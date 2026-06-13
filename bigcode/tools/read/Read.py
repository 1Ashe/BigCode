"""Read 工具实现。

学习思路：它读取 UTF-8 文本文件，加上行号返回，并把这次读取的快照记录到 ReadFileState。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.paths import resolve_path
from bigcode.tools.permissions import build_permission_target, check_content_policy, check_mode_policy_for_target
from bigcode.tools.read_file_state import digest_text, make_snapshot


class ReadInput(BaseModel):
    """工具输入模型。

    Pydantic 会根据这些字段校验模型传进来的参数，字段类型就是最重要的阅读线索。
    """
    file_path: str
    offset: int | None = Field(default=None, ge=0)
    limit: int | None = Field(default=None, ge=1)


class ReadTool(BaseTool[ReadInput, dict]):
    """一个可被模型调用的工具类。

    ToolRunner 会先校验 input_model 和权限，再调用这个类的 call() 方法执行真正逻辑。
    """
    name = "Read"
    aliases = ("ReadFile",)
    description = "Read a UTF-8 text file from the workspace."
    input_model = ReadInput
    permission_category = "read"
    state_effect = "read_file_state"
    max_result_chars = 120_000

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return True

    def is_concurrency_safe(self, input: ReadInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: ReadInput, ctx: ToolExecutionContext) -> ValidationResult:
        try:
            resolved = resolve_path(input.file_path, ctx.cwd, ctx.workspace_roots, must_exist=True)
        except Exception as exc:
            return ValidationResult(False, str(exc))
        if not resolved.resolved.is_file():
            return ValidationResult(False, "Read only supports regular files.")
        return ValidationResult(True)

    async def check_permissions(self, input: ReadInput, ctx: ToolExecutionContext) -> PermissionDecision:
        target = build_permission_target(self, input)
        decision = check_content_policy(target, ctx)
        if decision:
            return decision
        decision = check_mode_policy_for_target(target, ctx)
        if decision:
            return decision
        try:
            resolved = resolve_path(input.file_path, ctx.cwd, ctx.workspace_roots, must_exist=True)
        except Exception as exc:
            return PermissionDecision("deny", message=str(exc), reason="path-resolution", updated_input=input, decision_reason={"type": "safetyCheck"})
        if resolved.inside_workspace:
            return PermissionDecision("allow", message="Workspace read allowed.", updated_input=input)
        return PermissionDecision("ask", message="Read permission required.", updated_input=input)

    async def call(self, input: ReadInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        """工具执行入口。

        input 是已经通过 Pydantic 校验的参数，ctx 提供 cwd、权限、会话状态等运行环境。
        """
        resolved = resolve_path(input.file_path, ctx.cwd, ctx.workspace_roots, must_exist=True)
        if not resolved.resolved.is_file():
            raise RuntimeError("Read only supports regular files.")

        # 如果完全相同的文件范围已经读过，并且磁盘没变化，就不重复塞内容进上下文。
        # 这能显著减少模型上下文浪费。
        hit = ctx.read_file_state.check_duplicate_read(resolved.resolved, input.offset, input.limit)
        if hit:
            return ToolResult({"type": "file_unchanged", "file_path": str(input.file_path), "message": hit.message})
        raw = resolved.resolved.read_text(encoding="utf-8", errors="replace")
        lines = raw.splitlines()

        # offset/limit 是按“行”切片，不是按字符切片。返回内容会带 1-based 行号，
        # 方便模型后续用 Edit 时定位 old_string。
        start = input.offset or 0
        selected = lines[start : start + input.limit] if input.limit is not None else lines[start:]
        content = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(selected, start=start))
        partial = input.offset is not None or input.limit is not None

        # 读完后立刻记录快照，后续 Edit/Write 会用它确认文件没有被外部改动。
        ctx.read_file_state.record_read(
            resolved.resolved,
            make_snapshot(
                resolved.resolved,
                content=raw if not partial else content,
                content_digest=digest_text(raw if not partial else content),
                offset=input.offset,
                limit=input.limit,
                source="read",
                is_partial_view=partial,
            ),
        )
        return ToolResult(
            {
                "type": "text",
                "file_path": str(resolved.resolved),
                "content": content,
                "line_count": len(lines),
                "partial": partial,
            }
        )
