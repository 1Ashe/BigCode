"""Write 工具实现。

学习思路：创建或覆盖文件时会限制在工作区内；覆盖已有文件通常要求先读过并确认未变。
"""
from __future__ import annotations

from pydantic import BaseModel

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.paths import resolve_path
from bigcode.tools.permissions import build_permission_target, check_content_policy, check_mode_policy_for_target


class WriteInput(BaseModel):
    """工具输入模型。

    Pydantic 会根据这些字段校验模型传进来的参数，字段类型就是最重要的阅读线索。
    """
    file_path: str
    content: str


class WriteTool(BaseTool[WriteInput, dict]):
    """一个可被模型调用的工具类。

    ToolRunner 会先校验 input_model 和权限，再调用这个类的 call() 方法执行真正逻辑。
    """
    name = "Write"
    description = "Create or overwrite a workspace text file."
    input_model = WriteInput
    permission_category = "write"
    state_effect = "workspace_write"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return True

    def is_concurrency_safe(self, input: WriteInput, ctx: ToolExecutionContext) -> bool:
        return False

    async def validate_input(self, input: WriteInput, ctx: ToolExecutionContext) -> ValidationResult:
        try:
            resolved = resolve_path(input.file_path, ctx.cwd, ctx.workspace_roots, must_exist=False)
        except Exception as exc:
            return ValidationResult(False, str(exc))
        if not resolved.inside_workspace:
            return ValidationResult(False, "Write target is outside workspace.")
        return ValidationResult(True)

    async def check_permissions(self, input: WriteInput, ctx: ToolExecutionContext) -> PermissionDecision:
        target = build_permission_target(self, input)
        decision = check_content_policy(target, ctx)
        if decision:
            return decision
        decision = check_mode_policy_for_target(target, ctx)
        if decision:
            return decision
        try:
            resolved = resolve_path(input.file_path, ctx.cwd, ctx.workspace_roots, must_exist=False)
        except Exception as exc:
            return PermissionDecision("deny", message=str(exc), reason="path-resolution", updated_input=input, decision_reason={"type": "safetyCheck"})
        if not resolved.inside_workspace:
            return PermissionDecision("deny", message="Write target is outside workspace.", reason="workspace", updated_input=input, decision_reason={"type": "safetyCheck"})
        if ctx.permission_context.mode == "acceptEdits":
            return PermissionDecision("allow", message="Workspace write allowed by acceptEdits mode.", updated_input=input, decision_reason={"type": "mode"})
        return PermissionDecision("passthrough", updated_input=input)

    async def call(self, input: WriteInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        """工具执行入口。

        input 是已经通过 Pydantic 校验的参数，ctx 提供 cwd、权限、会话状态等运行环境。
        """
        resolved = resolve_path(input.file_path, ctx.cwd, ctx.workspace_roots, must_exist=False)
        if not resolved.inside_workspace:
            raise RuntimeError("Write target is outside workspace.")
        lock = ctx.read_file_state.lock_for(resolved.resolved)
        with lock:
            if resolved.exists:
                ctx.read_file_state.validate_unchanged(resolved.resolved)
            resolved.parent_resolved.mkdir(parents=True, exist_ok=True)
            tmp = resolved.resolved.with_suffix(resolved.resolved.suffix + ".tmp")
            tmp.write_text(input.content, encoding="utf-8")
            tmp.replace(resolved.resolved)
            snapshot = ctx.read_file_state.refresh_after_write(resolved.resolved, input.content)
        return ToolResult({"file_path": str(resolved.resolved), "bytes": len(input.content.encode("utf-8"))}, {"snapshot": snapshot})
