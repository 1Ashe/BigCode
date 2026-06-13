"""基于字符串替换的编辑工具。

学习思路：编辑前会确认文件已经读过且未被外部改动，避免覆盖用户刚改的内容。
"""
from __future__ import annotations

from pydantic import BaseModel

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.paths import resolve_path
from bigcode.tools.permissions import build_permission_target, check_content_policy, check_mode_policy_for_target


class EditInput(BaseModel):
    """工具输入模型。

    Pydantic 会根据这些字段校验模型传进来的参数，字段类型就是最重要的阅读线索。
    """
    file_path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class EditTool(BaseTool[EditInput, dict]):
    """一个可被模型调用的工具类。

    ToolRunner 会先校验 input_model 和权限，再调用这个类的 call() 方法执行真正逻辑。
    """
    name = "Edit"
    description = "Replace text in a file that has already been read."
    input_model = EditInput
    permission_category = "edit"
    state_effect = "workspace_write"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return True

    def is_concurrency_safe(self, input: EditInput, ctx: ToolExecutionContext) -> bool:
        return False

    async def validate_input(self, input: EditInput, ctx: ToolExecutionContext) -> ValidationResult:
        if input.old_string == "":
            return ValidationResult(False, "old_string must not be empty.")
        try:
            resolved = resolve_path(input.file_path, ctx.cwd, ctx.workspace_roots, must_exist=True)
        except Exception as exc:
            return ValidationResult(False, str(exc))
        if not resolved.resolved.is_file():
            return ValidationResult(False, "Edit only supports regular files.")
        return ValidationResult(True)

    async def check_permissions(self, input: EditInput, ctx: ToolExecutionContext) -> PermissionDecision:
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
        if not resolved.inside_workspace:
            return PermissionDecision("deny", message="Edit target is outside workspace.", reason="workspace", updated_input=input, decision_reason={"type": "safetyCheck"})
        if ctx.permission_context.mode == "acceptEdits":
            return PermissionDecision("allow", message="Workspace edit allowed by acceptEdits mode.", updated_input=input, decision_reason={"type": "mode"})
        return PermissionDecision("passthrough", updated_input=input)

    async def call(self, input: EditInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        """工具执行入口。

        input 是已经通过 Pydantic 校验的参数，ctx 提供 cwd、权限、会话状态等运行环境。
        """
        resolved = resolve_path(input.file_path, ctx.cwd, ctx.workspace_roots, must_exist=True)
        if not resolved.resolved.is_file():
            raise RuntimeError("Edit only supports regular files.")
        lock = ctx.read_file_state.lock_for(resolved.resolved)
        with lock:
            ctx.read_file_state.validate_unchanged(resolved.resolved)
            content = resolved.resolved.read_text(encoding="utf-8", errors="replace")
            count = content.count(input.old_string)
            if count == 0:
                raise RuntimeError("old_string was not found.")
            if count > 1 and not input.replace_all:
                raise RuntimeError("old_string appears multiple times; set replace_all=true.")
            new_content = content.replace(input.old_string, input.new_string) if input.replace_all else content.replace(input.old_string, input.new_string, 1)
            tmp = resolved.resolved.with_suffix(resolved.resolved.suffix + ".tmp")
            tmp.write_text(new_content, encoding="utf-8")
            tmp.replace(resolved.resolved)
            snapshot = ctx.read_file_state.refresh_after_write(resolved.resolved, new_content)
        return ToolResult({"file_path": str(resolved.resolved), "replacements": count if input.replace_all else 1}, {"snapshot": snapshot})
