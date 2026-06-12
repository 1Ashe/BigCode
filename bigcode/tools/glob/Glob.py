"""文件 glob 查找工具。

学习思路：它在工作区内按通配符找文件，适合先快速定位候选文件。
"""
from __future__ import annotations

from pydantic import BaseModel

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.paths import resolve_path
from bigcode.tools.permissions import build_permission_target, check_content_policy, check_mode_policy_for_target


class GlobInput(BaseModel):
    """工具输入模型。

    Pydantic 会根据这些字段校验模型传进来的参数，字段类型就是最重要的阅读线索。
    """
    pattern: str
    path: str | None = None


class GlobTool(BaseTool[GlobInput, dict]):
    """一个可被模型调用的工具类。

    ToolRunner 会先校验 input_model 和权限，再调用这个类的 call() 方法执行真正逻辑。
    """
    name = "Glob"
    description = "Find files by glob pattern inside the workspace."
    input_model = GlobInput
    permission_category = "read"
    state_effect = "none"

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return True

    def is_concurrency_safe(self, input: GlobInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: GlobInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not input.pattern:
            return ValidationResult(False, "pattern must not be empty.")
        try:
            base = resolve_path(input.path or ".", ctx.cwd, ctx.workspace_roots, must_exist=True).resolved
        except Exception as exc:
            return ValidationResult(False, str(exc))
        if not base.is_dir():
            return ValidationResult(False, "Glob path must be a directory.")
        return ValidationResult(True)

    async def check_permissions(self, input: GlobInput, ctx: ToolExecutionContext) -> PermissionDecision:
        target = build_permission_target(self, input)
        decision = check_content_policy(target, ctx)
        if decision:
            return decision
        decision = check_mode_policy_for_target(target, ctx)
        if decision:
            return decision
        try:
            resolved = resolve_path(input.path or ".", ctx.cwd, ctx.workspace_roots, must_exist=True)
        except Exception as exc:
            return PermissionDecision("deny", message=str(exc), reason="path-resolution", updated_input=input, decision_reason={"type": "safetyCheck"})
        if resolved.inside_workspace:
            return PermissionDecision("allow", message="Workspace read allowed.", updated_input=input)
        return PermissionDecision("ask", message="Read permission required.", updated_input=input)

    async def call(self, input: GlobInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        """工具执行入口。

        input 是已经通过 Pydantic 校验的参数，ctx 提供 cwd、权限、会话状态等运行环境。
        """
        base = resolve_path(input.path or ".", ctx.cwd, ctx.workspace_roots, must_exist=True).resolved
        if not base.is_dir():
            raise RuntimeError("Glob path must be a directory.")
        matches = sorted(str(p.relative_to(ctx.cwd)) if _inside(p, ctx.cwd) else str(p) for p in base.glob(input.pattern) if p.is_file())
        return ToolResult({"matches": matches[:1000], "truncated": len(matches) > 1000, "count": len(matches)})


def _inside(path, root) -> bool:
    """判断 path 是否在 root 目录下。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
