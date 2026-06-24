"""Bash 工具实现。

学习思路：这里只负责运行命令和收集输出；命令是否允许执行由 ToolRunner 调用 permissions.py 判断。
"""
from __future__ import annotations

import asyncio
import re

from pydantic import BaseModel, Field

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.permissions import build_permission_target, check_content_policy, check_mode_policy_for_target, classify_bash
from bigcode.tools.permissions.models import PermissionTarget
from bigcode.tools.permissions.pipeline import _engine_from_context


class BashInput(BaseModel):
    """工具输入模型。

    Pydantic 会根据这些字段校验模型传进来的参数，字段类型就是最重要的阅读线索。
    """
    command: str
    timeout: int = Field(default=30, ge=1, le=600)
    dangerously_disable_sandbox: bool = Field(default=False)


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

        # 1. Content-level deny/ask/allow rules
        decision = check_content_policy(target, ctx)
        if decision:
            return decision

        # 2. Mode policy (plan mode restrictions)
        decision = check_mode_policy_for_target(target, ctx, self)
        if decision:
            return decision

        # 3. AutoAllow: when sandbox is active and autoAllow enabled,
        #    convert "ask" to "allow" but preserve explicit rules
        if _should_auto_allow_bash(input, target, ctx):
            return PermissionDecision(
                "allow",
                message="Auto-allowed with sandbox (OS-level sandbox active).",
                updated_input=input,
                decision_reason={"type": "mode", "sandboxAutoAllow": True},
            )

        # 4. Safety classification
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
        from bigcode.sandbox import BubblewrapBuilder, scrub_after_command, should_use_sandbox

        sandbox_config = getattr(ctx, "sandbox_config", None)
        use_sandbox = should_use_sandbox(
            command=input.command,
            dangerously_disable_sandbox=input.dangerously_disable_sandbox,
            config=sandbox_config,
        )

        if use_sandbox and sandbox_config:
            tmp_dir = _create_sandbox_tmp_dir(ctx)
            builder = BubblewrapBuilder(sandbox_config)
            cmd_args = builder.build(
                input.command,
                shell_path="/bin/bash",
                chdir=str(ctx.cwd),
                tmp_dir=tmp_dir,
            )
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
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

        if use_sandbox and sandbox_config and sandbox_config.scrub_paths:
            scrub_after_command(sandbox_config.scrub_paths)

        stderr_text = stderr.decode(errors="replace")[-100000:]
        if use_sandbox:
            stderr_text = _annotate_sandbox_failures(stderr_text)

        return ToolResult(
            {
                "command": input.command,
                "exit_code": proc.returncode,
                # 只保留尾部输出。多数命令的错误线索在末尾，也能避免超大输出撑爆上下文。
                "stdout": stdout.decode(errors="replace")[-100000:],
                "stderr": stderr_text,
            }
        )


# ---------------------------------------------------------------------------
# Sandbox helpers
# ---------------------------------------------------------------------------


def _should_auto_allow_bash(
    input: BashInput, target: PermissionTarget, ctx: ToolExecutionContext
) -> bool:
    """Return True if this Bash command should be auto-allowed because the
    OS-level sandbox provides real enforcement.

    Explicit deny/ask rules and dangerous commands ALWAYS override autoAllow.
    """
    sandbox_config = getattr(ctx, "sandbox_config", None)
    if sandbox_config is None or not sandbox_config.enabled:
        return False
    if not sandbox_config.auto_allow_bash_if_sandboxed:
        return False
    if input.dangerously_disable_sandbox:
        return False

    # Explicit deny rules override autoAllow
    engine = _engine_from_context(ctx)
    if engine.match_content(target, "deny") or engine.match_tool(target, "deny"):
        return False

    # Explicit ask rules override autoAllow
    if engine.match_content(target, "ask") or engine.match_tool(target, "ask"):
        return False

    # Dangerous commands are never auto-allowed
    if classify_bash(input.command) == "danger":
        return False

    return True


def _create_sandbox_tmp_dir(ctx: ToolExecutionContext) -> str:
    """Create a temporary directory for sandbox TMPDIR."""
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="bigcode-sandbox-")
    return tmp_dir


# Patterns that indicate a sandbox-related failure (not a transient error)
_SANDBOX_FAILURE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Permission denied"), "filesystem write restriction"),
    (re.compile(r"Read-only file system"), "filesystem write restriction"),
    (
        re.compile(r"Network is unreachable|Could not resolve|Temporary failure in name resolution"),
        "network isolation",
    ),
    (re.compile(r"Operation not permitted"), "sandbox restriction"),
]


def _annotate_sandbox_failures(stderr: str) -> str:
    """If stderr contains signals of a sandbox-enforced failure, append an
    explanation so the model understands this is a security boundary, not a
    transient error it should retry with different approaches."""
    if not stderr.strip():
        return stderr
    matched = None
    for pattern, kind in _SANDBOX_FAILURE_PATTERNS:
        if pattern.search(stderr):
            matched = kind
            break
    if matched is None:
        return stderr
    return (
        stderr.rstrip()
        + f"\n\n[This command failed due to a {matched} enforced by the OS-level"
        " sandbox (bubblewrap). The sandbox restricts filesystem writes to"
        " allowed paths and blocks network access when configured. Do not retry"
        " with different approaches — this is a security boundary, not a"
        " transient error. Use the dedicated Read/Write/Edit tools for file"
        " operations, or ask the user to adjust sandbox settings.]"
    )
