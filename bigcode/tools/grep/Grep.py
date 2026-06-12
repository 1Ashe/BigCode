"""文本搜索工具。

学习思路：它递归读取文本文件并返回最多 1000 条匹配，适合找函数名、配置项或错误信息。
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.paths import resolve_path
from bigcode.tools.permissions import build_permission_target, check_content_policy, check_mode_policy_for_target


class GrepInput(BaseModel):
    """工具输入模型。

    Pydantic 会根据这些字段校验模型传进来的参数，字段类型就是最重要的阅读线索。
    """
    pattern: str
    path: str | None = None
    glob: str | None = None
    regex: bool = True


class GrepTool(BaseTool[GrepInput, dict]):
    """一个可被模型调用的工具类。

    ToolRunner 会先校验 input_model 和权限，再调用这个类的 call() 方法执行真正逻辑。
    """
    name = "Grep"
    description = "Search text files inside the workspace."
    input_model = GrepInput
    permission_category = "read"
    state_effect = "none"
    max_result_chars = 100_000

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return True

    def is_concurrency_safe(self, input: GrepInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: GrepInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not input.pattern:
            return ValidationResult(False, "pattern must not be empty.")
        if input.regex:
            try:
                re.compile(input.pattern)
            except re.error as exc:
                return ValidationResult(False, f"Invalid regex: {exc}")
        try:
            resolve_path(input.path or ".", ctx.cwd, ctx.workspace_roots, must_exist=True)
        except Exception as exc:
            return ValidationResult(False, str(exc))
        return ValidationResult(True)

    async def check_permissions(self, input: GrepInput, ctx: ToolExecutionContext) -> PermissionDecision:
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

    async def call(self, input: GrepInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        """工具执行入口。

        input 是已经通过 Pydantic 校验的参数，ctx 提供 cwd、权限、会话状态等运行环境。
        """
        base = resolve_path(input.path or ".", ctx.cwd, ctx.workspace_roots, must_exist=True).resolved
        paths = [base] if base.is_file() else list(base.rglob(input.glob or "*"))
        matcher = re.compile(input.pattern) if input.regex else None
        matches = []
        for path in paths:
            if len(matches) >= 1000 or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for idx, line in enumerate(text.splitlines(), start=1):
                ok = bool(matcher.search(line)) if matcher else input.pattern in line
                if ok:
                    matches.append({"file": str(path), "line": idx, "text": line[:500]})
                    if len(matches) >= 1000:
                        break
        return ToolResult({"matches": matches, "truncated": len(matches) >= 1000, "count": len(matches)})
