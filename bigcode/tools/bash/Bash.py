"""Bash 工具实现。

学习思路：这里只负责运行命令和收集输出；命令是否允许执行由 ToolRunner 调用 permissions.py 判断。
"""
from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permissions import build_permission_target, check_content_policy, check_mode_policy_for_target, classify_bash


class BashInput(BaseModel):
    """工具输入模型。

    Pydantic 会根据这些字段校验模型传进来的参数，字段类型就是最重要的阅读线索。
    """
    command: str
    timeout: int = Field(default=30, ge=1, le=600)


class BashTool(BaseTool[BashInput, dict]):
    """一个可被模型调用的工具类。

    ToolRunner 会先校验 input_model 和权限，再调用这个类的 call() 方法执行真正逻辑。
    """
    name = "Bash"
    description = (
        "Run a shell command from the workspace. Use this for tests, builds, package managers, git inspection, "
        "or system commands that dedicated tools cannot perform. Prefer Read/Glob/Grep/Edit/Write for file "
        "operations. Include a focused command and timeout. Read-only commands can run directly; mutating, "
        "unknown, or risky commands may require permission, and dangerous commands are denied."
    )
    input_model = BashInput
    permission_category = "bash"
    state_effect = "external"
    max_result_chars = 120_000

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return True

    def is_concurrency_safe(self, input: BashInput, ctx: ToolExecutionContext) -> bool:
        return classify_bash(input.command) == "read"

    def is_read_only(self, input: BashInput, ctx: ToolExecutionContext) -> bool:
        return classify_bash(input.command) == "read"

    async def validate_input(self, input: BashInput, ctx: ToolExecutionContext) -> ValidationResult:
        if not input.command.strip():
            return ValidationResult(False, "command must not be empty.")
        return ValidationResult(True)

    async def check_permissions(self, input: BashInput, ctx: ToolExecutionContext) -> PermissionDecision:
        target = build_permission_target(self, input)
        decision = check_content_policy(target, ctx)
        if decision:
            return decision
        decision = check_mode_policy_for_target(target, ctx, self)
        if decision:
            return decision
        kind = classify_bash(input.command)
        if kind == "danger":
            return PermissionDecision("deny", message="Dangerous shell command denied.", reason="bash-danger", decision_reason={"type": "safetyCheck"})
        if kind == "unknown":
            return PermissionDecision("ask", message="Shell command is not provably read-only.", reason="bash-unknown", decision_reason={"type": "safetyCheck"})
        if kind == "read":
            return PermissionDecision("allow", message="Read-only Bash allowed.", updated_input=input)
        return PermissionDecision("passthrough", updated_input=input)

    async def call(self, input: BashInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        """工具执行入口。

        input 是已经通过 Pydantic 校验的参数，ctx 提供 cwd、权限、会话状态等运行环境。
        """
        # 权限判断已经在 ToolRunner/permissions.py 完成，这里只负责执行命令。
        # create_subprocess_shell 会交给系统 shell 解析字符串命令。
        proc = await asyncio.create_subprocess_shell(
            input.command,
            cwd=ctx.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=input.timeout)
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        except asyncio.TimeoutError:
            # 超时必须杀进程并 wait，避免留下后台子进程。
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"Command timed out after {input.timeout}s")
        return ToolResult(
            {
                "command": input.command,
                "exit_code": proc.returncode,
                # 只保留尾部输出。多数命令的错误线索在末尾，也能避免超大输出撑爆上下文。
                "stdout": stdout.decode(errors="replace")[-100000:],
                "stderr": stderr.decode(errors="replace")[-100000:],
            }
        )
