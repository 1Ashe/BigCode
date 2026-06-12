"""工具权限决策核心。"""
from __future__ import annotations

import fnmatch
import ipaddress
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel

from .base import BaseTool, PermissionDecision, ToolExecutionContext


PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"]


@dataclass
class PermissionRule:
    """一条权限规则。

    pattern 为空表示整工具规则；pattern 非空表示内容级规则。
    """

    tool_name: str
    behavior: Literal["allow", "deny", "ask"]
    pattern: str | None = None
    source: str = "session"
    reason: str = ""


@dataclass
class ToolPermissionContext:
    """当前会话的权限模式和显式 allow/deny/ask 规则。"""

    mode: PermissionMode = "default"
    always_allow: list[PermissionRule] = field(default_factory=list)
    always_deny: list[PermissionRule] = field(default_factory=list)
    always_ask: list[PermissionRule] = field(default_factory=list)
    should_avoid_permission_prompts: bool = False


@dataclass(frozen=True)
class PermissionTarget:
    """从具体工具输入中抽象出的权限判断对象。"""

    tool_name: str
    category: str
    path: Path | None = None
    command: str | None = None
    network_url: str | None = None
    raw: BaseModel | None = None


SENSITIVE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
}
SYSTEM_DENY_PREFIXES = [Path(p) for p in ["/bin", "/sbin", "/usr", "/etc", "/var", "/boot", "/dev", "/proc", "/sys"]]
READ_ONLY_BASH = {
    "pwd",
    "ls",
    "find",
    "rg",
    "grep",
    "cat",
    "sed",
    "head",
    "tail",
    "wc",
    "git",
}
MUTATING_BASH = {
    "rm",
    "mv",
    "cp",
    "chmod",
    "chown",
    "touch",
    "mkdir",
    "rmdir",
    "npm",
    "pip",
    "conda",
    "uv",
    "cargo",
    "go",
    "make",
    "docker",
    "kubectl",
    "git",
}
COMPLEX_SHELL_RE = re.compile(r"(&&|\|\||;|\||>|<|\$\(|`|\n|\*)")
PLAN_ALLOWED_TOOLS = {
    "Read",
    "Glob",
    "Grep",
    "ArtifactRead",
    "PlanShow",
    "WritePlan",
    "ExitPlanMode",
    "AskUserQuestion",
    "TaskList",
    "TaskGet",
    "TaskOutput",
    "SkillLoad",
    "SkillResourceRead",
    "ExternalResourceList",
    "ExternalResourceRead",
    "ExternalPromptList",
    "ExternalPromptGet",
}
READ_ONLY_SANDBOX_TOOLS = {
    "Read",
    "Glob",
    "Grep",
    "ArtifactRead",
    "PlanShow",
    "TaskList",
    "TaskGet",
    "TaskOutput",
    "SkillLoad",
    "SkillResourceRead",
    "ExternalResourceList",
    "ExternalResourceRead",
    "ExternalPromptList",
    "ExternalPromptGet",
}


async def decide_permission(
    tool: BaseTool,
    input_model: BaseModel,
    ctx: ToolExecutionContext,
    *,
    hook_decision: PermissionDecision | None = None,
) -> PermissionDecision:
    """按整工具规则、工具内容检查、bypass、整工具 allow 的顺序收敛权限。"""

    target = build_permission_target(tool, input_model)

    # 1. 整工具 deny。只看工具名，不看参数。
    whole_rule = _match_tool_rule(ctx.permission_context.always_deny, target)
    if whole_rule:
        return _decision_from_rule(whole_rule, "deny", "Denied by explicit tool rule.")

    # 2. 整工具 ask。命中后不执行工具内部检查，也不允许 bypass/allow 放宽。
    whole_rule = _match_tool_rule(ctx.permission_context.always_ask, target)
    if whole_rule:
        return _decision_from_rule(whole_rule, "ask", "Permission required by explicit tool rule.")

    # 3. 工具检查具体输入。工具负责内容级 deny/safety/ask/allow。
    tool_decision = await tool.check_permissions(input_model, ctx)
    if tool_decision.updated_input is None:
        tool_decision.updated_input = input_model

    if tool_decision.behavior == "passthrough":
        # 工具没有明确结论时，通用层按模式和工具类别给出普通决策。
        decision = _apply_generic_defaults(tool_decision, tool, target, ctx)
    else:
        decision = tool_decision

    # 4. 不可放宽结果直接返回。
    if _is_unrelaxable(decision):
        return decision

    sandbox_decision = _check_sandbox_profile(target, ctx)
    if sandbox_decision:
        return sandbox_decision

    # PreToolUse hook 的 approve/ask 只参与剩余普通结果，不覆盖前面的 deny/特殊 ask。
    if hook_decision and hook_decision.behavior in {"allow", "ask"}:
        hook_decision.decision_reason.setdefault("type", "hook")
        decision = hook_decision

    # 5. bypassPermissions 只放宽普通 ask/passthrough。
    if ctx.permission_context.mode == "bypassPermissions":
        return PermissionDecision("allow", message="Allowed by bypassPermissions mode.", updated_input=input_model, decision_reason={"type": "mode"})

    # 6. 整工具 allow 只能放宽剩余普通结果。
    whole_rule = _match_tool_rule(ctx.permission_context.always_allow, target)
    if whole_rule:
        return _decision_from_rule(whole_rule, "allow", "Allowed by explicit tool rule.")

    # 7. 使用工具/通用层剩余结果；passthrough 没有自动允许依据，转 ask。
    if decision.behavior == "allow":
        return decision
    if decision.behavior == "ask":
        return decision
    if decision.behavior == "passthrough":
        return PermissionDecision(
            "ask",
            message=f"{tool.name} requires permission.",
            updated_input=input_model,
            decision_reason={"type": "ordinary"},
        )
    return decision


def build_permission_target(tool: BaseTool, input_model: BaseModel) -> PermissionTarget:
    """从 Pydantic 输入模型中抽取 path/command/url 等权限判断字段。"""

    data = input_model.model_dump()
    path_value = data.get("file_path") or data.get("path")
    return PermissionTarget(
        tool_name=tool.name,
        category=tool.permission_category,
        path=Path(path_value) if path_value else None,
        command=data.get("command") or data.get("subagent_type"),
        network_url=data.get("url"),
        raw=input_model,
    )


def check_content_policy(target: PermissionTarget, ctx: ToolExecutionContext) -> PermissionDecision | None:
    """工具内部可调用的标准内容级权限顺序。"""

    rule = _match_content_rule(ctx.permission_context.always_deny, target)
    if rule:
        return _decision_from_rule(rule, "deny", "Denied by explicit content rule.")

    safety = check_safety_for_target(target, ctx)
    if safety:
        return safety

    rule = _match_content_rule(ctx.permission_context.always_ask, target)
    if rule:
        return _decision_from_rule(rule, "ask", "Permission required by explicit content rule.")

    rule = _match_content_rule(ctx.permission_context.always_allow, target)
    if rule:
        return _decision_from_rule(rule, "allow", "Allowed by explicit content rule.")

    return None


def check_mode_policy_for_target(target: PermissionTarget, ctx: ToolExecutionContext) -> PermissionDecision | None:
    """工具返回精确 allow 前可调用的模式/状态收紧检查。"""

    if ctx.permission_context.mode == "plan":
        if target.tool_name not in PLAN_ALLOWED_TOOLS:
            if target.tool_name == "Bash" and target.command and classify_bash(target.command) == "read":
                return None
            if target.tool_name == "Agent" and getattr(target.raw, "subagent_type", "") in {"explorer", "planAgent"}:
                return None
            return PermissionDecision(
                "deny",
                message=f"{target.tool_name} is not allowed in Plan Mode.",
                reason="plan-mode",
                updated_input=target.raw,
                decision_reason={"type": "mode"},
            )
    return _check_sandbox_profile_target(target, ctx)


def check_safety_for_target(target: PermissionTarget, ctx: ToolExecutionContext) -> PermissionDecision | None:
    """执行不能被配置或 bypass 绕过的输入级安全检查。"""

    message = check_hard_deny(target, ctx)
    if message:
        return PermissionDecision("deny", message=message, reason=message, decision_reason={"type": "safetyCheck"})
    if target.tool_name == "Bash" and target.command:
        kind = classify_bash(target.command)
        if kind == "danger":
            return PermissionDecision("deny", message="Dangerous shell command denied.", reason="bash-danger", decision_reason={"type": "safetyCheck"})
        if kind == "unknown":
            return PermissionDecision("ask", message="Shell command is not provably read-only.", reason="bash-unknown", decision_reason={"type": "safetyCheck"})
    return None


def check_hard_deny(target: PermissionTarget, ctx: ToolExecutionContext) -> str | None:
    """执行不能被配置绕过的安全拒绝。"""

    if target.path is not None:
        name = target.path.name
        if name in SENSITIVE_NAMES or name.endswith(".pem"):
            return f"Access to sensitive file {name!r} is denied."
        try:
            resolved = _resolve_existing_or_parent(ctx.cwd, target.path)
        except Exception as exc:
            return f"Path could not be resolved safely: {exc}"
        if target.category in {"write", "edit", "delete"}:
            for prefix in SYSTEM_DENY_PREFIXES:
                if _is_relative_to(resolved, prefix):
                    return f"Writes to system path {prefix} are denied."
    if target.command:
        command = target.command.strip()
        if re.search(r"(^|\s)(sudo|su)(\s|$)", command):
            return "Privilege escalation commands are denied."
        if re.search(r"rm\s+-[^\n]*r[^\n]*f\s+(/|~|\$HOME)(\s|$)", command):
            return "Broad recursive deletion is denied."
    if target.network_url:
        parsed = urlparse(target.network_url)
        if parsed.scheme not in {"http", "https"}:
            return "Only http and https URLs are allowed."
        if not parsed.hostname:
            return "URL must include a host."
        if _is_unsafe_network_host(parsed.hostname):
            return "Localhost, metadata, and private network targets are denied."
    return None


def classify_bash(command: str) -> Literal["read", "mutate", "danger", "unknown"]:
    """把 shell 命令粗分为 read/mutate/danger/unknown。"""

    command = command.strip()
    if not command:
        return "read"
    if re.search(r"(^|\s)(sudo|su)(\s|$)", command):
        return "danger"
    if COMPLEX_SHELL_RE.search(command):
        return "unknown"
    try:
        parts = shlex.split(command)
    except ValueError:
        return "unknown"
    if not parts:
        return "read"
    exe = Path(parts[0]).name
    if exe == "git" and len(parts) > 1:
        sub = parts[1]
        if sub == "branch":
            if any(part in {"-d", "-D", "--delete", "--move", "-m", "-M", "--copy", "-c", "-C"} for part in parts[2:]):
                return "mutate"
            read_flags = {"-a", "-r", "-v", "-vv", "--all", "--remotes", "--verbose", "--list", "--show-current", "--contains", "--merged", "--no-merged"}
            return "read" if all(part.startswith("-") and part in read_flags for part in parts[2:]) else ("read" if len(parts) == 2 else "mutate")
        if sub in {"status", "diff", "show", "log", "rev-parse", "ls-files", "grep", "blame"}:
            return "read"
        return "mutate"
    if exe == "find":
        if any(part in {"-delete", "-exec", "-execdir", "-ok", "-okdir"} for part in parts[1:]):
            return "mutate"
        return "read"
    if exe == "sed":
        if any(part == "-i" or part.startswith("-i") for part in parts[1:]):
            return "mutate"
        return "read"
    if exe in {"python", "python3", "node", "ruby", "perl", "bash", "sh"}:
        if len(parts) == 2 and parts[1] in {"--version", "-V", "-v"}:
            return "read"
        return "unknown"
    if exe in READ_ONLY_BASH:
        return "read"
    if exe in MUTATING_BASH:
        return "mutate"
    return "unknown"


def parse_permission_rule_string(raw: str, behavior: Literal["allow", "deny", "ask"], *, source: str = "config") -> PermissionRule:
    """解析配置字符串：Bash 是整工具规则，Bash(pattern) 是内容级规则。"""

    text = raw.strip()
    match = re.fullmatch(r"([^()]+)\((.*)\)", text)
    if match:
        return PermissionRule(tool_name=match.group(1).strip(), behavior=behavior, pattern=match.group(2), source=source)
    return PermissionRule(tool_name=text, behavior=behavior, source=source)


def _apply_generic_defaults(
    decision: PermissionDecision,
    tool: BaseTool,
    target: PermissionTarget,
    ctx: ToolExecutionContext,
) -> PermissionDecision:
    """把 passthrough 或普通结果交给通用权限层补足。"""

    if decision.behavior != "passthrough":
        return decision

    mode = ctx.permission_context.mode
    if mode == "plan":
        if tool.name not in PLAN_ALLOWED_TOOLS:
            if tool.name == "Bash" and target.command and classify_bash(target.command) == "read":
                return PermissionDecision("allow", message="Read-only Bash allowed in Plan Mode.", updated_input=target.raw, decision_reason={"type": "mode"})
            if tool.name == "Agent" and getattr(target.raw, "subagent_type", "") in {"explorer", "planAgent"}:
                return PermissionDecision("allow", message="Read-only planning subAgent allowed.", updated_input=target.raw, decision_reason={"type": "mode"})
            return PermissionDecision("deny", message=f"{tool.name} is not allowed in Plan Mode.", reason="plan-mode", updated_input=target.raw, decision_reason={"type": "mode"})
        if decision.behavior == "passthrough":
            return PermissionDecision("allow", message="Allowed in Plan Mode.", updated_input=target.raw, decision_reason={"type": "mode"})
        return decision

    if mode == "acceptEdits" and target.category in {"write", "edit"}:
        if target.path is not None:
            resolved = _resolve_existing_or_parent(ctx.cwd, target.path)
            if _inside_any(resolved, ctx.workspace_roots):
                return PermissionDecision("allow", message="Workspace edit allowed by acceptEdits mode.", updated_input=target.raw, decision_reason={"type": "mode"})

    if target.category == "read":
        if target.path is None:
            return PermissionDecision("allow", message="Read allowed.", updated_input=target.raw)
        resolved = _resolve_existing_or_parent(ctx.cwd, target.path)
        behavior = "allow" if _inside_any(resolved, ctx.workspace_roots) else "ask"
        return PermissionDecision(behavior, message="Read permission required.", updated_input=target.raw)
    if target.category == "skill":
        return PermissionDecision("allow", message="Registered skill access allowed.", updated_input=target.raw)
    if target.category == "state":
        return PermissionDecision("allow", message="App state tool allowed.", updated_input=target.raw)
    if target.category == "bash" and target.command and classify_bash(target.command) == "read":
        return PermissionDecision("allow", message="Read-only Bash allowed.", updated_input=target.raw)
    if target.category in {"write", "edit", "delete", "bash", "network", "agent", "mcp"}:
        return PermissionDecision("ask", message=f"{tool.name} requires permission.", updated_input=target.raw)
    return PermissionDecision("deny", message=f"Unknown permission category {target.category!r}.", updated_input=target.raw)


def _check_sandbox_profile(target: PermissionTarget, ctx: ToolExecutionContext) -> PermissionDecision | None:
    """按 sandbox profile 收紧普通权限结果，不能被 bypass 或 allow 放宽。"""

    return _check_sandbox_profile_target(target, ctx)


def _check_sandbox_profile_target(target: PermissionTarget, ctx: ToolExecutionContext) -> PermissionDecision | None:
    """按 sandbox profile 收紧普通权限结果，不能被 bypass 或 allow 放宽。"""

    profile = getattr(ctx, "sandbox_profile", "none")
    if profile == "none":
        return None
    kind = classify_bash(target.command) if target.command else None
    if profile == "read-only":
        if target.tool_name in READ_ONLY_SANDBOX_TOOLS:
            return None
        if target.tool_name == "Bash" and kind == "read":
            return None
        if target.tool_name == "Agent" and getattr(target.raw, "subagent_type", "") in {"explorer", "planAgent"}:
            return None
        return PermissionDecision(
            "deny",
            message=f"{target.tool_name} is denied by read-only sandbox profile.",
            reason="sandbox-read-only",
            updated_input=target.raw,
            decision_reason={"type": "safetyCheck"},
        )
    if profile == "workspace":
        if target.category == "network":
            return PermissionDecision(
                "deny",
                message=f"{target.tool_name} is denied by workspace sandbox profile.",
                reason="sandbox-workspace-network",
                updated_input=target.raw,
                decision_reason={"type": "safetyCheck"},
            )
        if target.tool_name == "Bash" and kind != "read":
            return PermissionDecision(
                "deny",
                message="Mutating Bash is denied by workspace sandbox profile.",
                reason="sandbox-workspace-bash",
                updated_input=target.raw,
                decision_reason={"type": "safetyCheck"},
            )
    return None


def _is_unrelaxable(decision: PermissionDecision) -> bool:
    """deny 和特殊 ask 不能被 bypass 或整工具 allow 覆盖。"""

    if decision.behavior == "deny":
        return True
    return decision.behavior == "ask" and decision.reason_type in {"rule", "safetyCheck", "requiresUserInteraction"}


def _decision_from_rule(rule: PermissionRule, behavior: Literal["allow", "deny", "ask"], fallback: str) -> PermissionDecision:
    return PermissionDecision(
        behavior,
        message=rule.reason or fallback,
        reason=rule.reason,
        rule=rule.source,
        decision_reason={"type": "rule", "rule": {"ruleBehavior": behavior, "source": rule.source, "toolName": rule.tool_name, "pattern": rule.pattern}},
    )


def _match_tool_rule(rules: list[PermissionRule], target: PermissionTarget) -> PermissionRule | None:
    for rule in rules:
        if rule.pattern is not None:
            continue
        if rule.tool_name in {"*", target.tool_name}:
            return rule
    return None


def _match_content_rule(rules: list[PermissionRule], target: PermissionTarget) -> PermissionRule | None:
    for rule in rules:
        if rule.pattern is None:
            continue
        if rule.tool_name not in {"*", target.tool_name}:
            continue
        if fnmatch.fnmatch(_target_haystack(target), rule.pattern):
            return rule
    return None


def _target_haystack(target: PermissionTarget) -> str:
    return target.command or str(target.path or "") or target.network_url or ""


def _resolve_existing_or_parent(cwd: Path, path: Path) -> Path:
    """解析已存在路径；新文件则解析其父目录再拼回文件名。"""

    candidate = path if path.is_absolute() else cwd / path
    if candidate.exists():
        return candidate.resolve(strict=True)
    return candidate.parent.resolve(strict=True) / candidate.name


def _inside_any(path: Path, roots: list[Path]) -> bool:
    """判断路径是否在任一允许根目录内部。"""

    return any(_is_relative_to(path, root) for root in roots)


def _is_relative_to(path: Path, root: Path) -> bool:
    """Path.relative_to 的安全包装，用于兼容不存在的路径。"""

    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _is_unsafe_network_host(hostname: str) -> bool:
    """判断 URL 主机是否是 localhost、私网、链路本地或保留地址。"""

    host = hostname.strip("[]").lower()
    if host in {"localhost", "localhost.localdomain", "metadata.google.internal"} or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_unspecified or ip.is_multicast or ip.is_reserved)
