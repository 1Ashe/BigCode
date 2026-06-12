"""把 settings.json 中配置的命令行 hook 接入 HookBus。

学习思路：外部命令通过 stdin 接收 HookInput JSON，可在 stdout 最后输出 JSON 来批准、阻止或修改工具输入。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bigcode.context.attachments import Attachment
from bigcode.utils.jsonio import read_json_file, to_jsonable

from .bus import HookHandler
from .models import HookInput, HookOutput


VALID_HOOK_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "ContextBuild",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
    "PlanModeEnter",
    "PlanModeExit",
    "TaskCreated",
    "TaskUpdated",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "CapabilityChanged",
}


@dataclass
class CommandHookSpec:
    """一个命令型 hook 的运行配置。

    command 是要执行的 shell 命令，timeout 控制最大等待时间，matcher 可限制工具名。
    """
    type: Literal["command"]
    command: str
    timeout: int = 60
    once: bool = False
    status_message: str | None = None
    matcher: str | None = None
    event: str = "PreToolUse"


CommandRegistrationStatus = Literal["enabled", "disabled", "failed"]


@dataclass(frozen=True)
class CommandRegistration:
    """配置解析后的 hook 注册记录。

    即使配置无效也会生成 failed/disabled 记录，doctor 命令才能报告问题。
    """
    id: str
    event: str
    matcher: str | None
    command: str
    timeout: int
    once: bool
    enabled: bool
    status: CommandRegistrationStatus
    reason: str = ""
    source: str = "settings"


class CommandHookHandler(HookHandler):
    """执行外部命令的 HookHandler。

    它把 HookInput JSON 发到命令 stdin，再解析命令 stdout 最后的 JSON 作为 HookOutput。
    """
    source = "user"

    def __init__(self, spec: CommandHookSpec) -> None:
        """根据 CommandHookSpec 设置 handler 名称和监听事件。"""
        self.spec = spec
        self.name = f"command:{spec.command}"
        self.events = (spec.event,)  # type: ignore[assignment]

    async def matches(self, input: HookInput) -> bool:
        """先匹配事件名，再用 matcher 限制 PreToolUse/PostToolUse 的工具名。"""
        if not await super().matches(input):
            return False
        if not self.spec.matcher:
            return True
        if input.hook_event_name in {"PreToolUse", "PermissionRequest", "PostToolUse"}:
            return input.payload.get("tool_name") == self.spec.matcher
        return True

    async def run(self, input: HookInput) -> HookOutput:
        """执行外部 hook 命令。

        HookInput 通过 stdin 传入，stdout 最后的 JSON 会被解析成 HookOutput。
        """
        # 命令 hook 是用户配置的外部进程。BigCode 把当前 HookInput 通过 stdin
        # 传给它，外部脚本可以根据工具名、参数、cwd 等信息决定怎么响应。
        proc = await asyncio.create_subprocess_shell(
            self.spec.command,
            cwd=input.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = json.dumps(to_jsonable(input), ensure_ascii=False).encode()
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout=self.spec.timeout)
        except asyncio.TimeoutError:
            # hook 超时也不让主流程挂死；杀掉子进程后，把错误作为 Attachment 返回。
            proc.kill()
            await proc.wait()
            return HookOutput(attachments=[Attachment(type="hook_execution_error", text=f"Hook timed out: {self.spec.command}")])
        out = stdout.decode(errors="replace")[-65536:]
        err = stderr.decode(errors="replace")[-65536:]
        parsed = _parse_last_json(out)
        if parsed:
            # 外部 hook 最灵活的返回方式：stdout 最后输出 JSON。
            # 允许它批准/阻止/要求询问，也允许改工具输入或追加上下文。
            return HookOutput(
                decision=parsed.get("decision", "passthrough"),
                reason=parsed.get("reason", ""),
                updated_input=parsed.get("updated_input"),
                additional_context=parsed.get("additional_context"),
                continue_turn=parsed.get("continue"),
            )
        if proc.returncode == 2:
            # 兼容简单脚本：退出码 2 表示阻止操作，stderr/stdout 作为原因。
            return HookOutput(decision="block", reason=(err or out or "Command hook blocked.").strip())
        if proc.returncode and proc.returncode != 0:
            return HookOutput(attachments=[Attachment(type="hook_execution_error", text=f"Hook failed ({proc.returncode}): {err or out}")])
        return HookOutput()


class CommandRegistry:
    """命令 hook 注册表。

    它负责从 settings/plugin_roots 生成注册记录，并转换为可运行的 CommandHookHandler。
    """
    def __init__(self, registrations: list[CommandRegistration] | None = None) -> None:
        """保存已经解析出的 hook 注册记录。"""
        self.registrations = registrations or []

    @classmethod
    def from_settings(cls, settings: dict, *, plugin_roots: list[Path] | None = None) -> "CommandRegistry":
        """从 settings 中解析命令 hook，并附加当前暂不支持的插件 command 报告。"""
        registrations = _registrations_from_settings(settings)
        registrations.extend(_unsupported_plugin_commands(plugin_roots or []))
        return cls(registrations)

    def enabled_handlers(self) -> list[CommandHookHandler]:
        """把 enabled 注册记录转换成真正可执行的 CommandHookHandler。"""
        handlers: list[CommandHookHandler] = []
        for registration in self.registrations:
            if registration.status != "enabled":
                continue
            handlers.append(
                CommandHookHandler(
                    CommandHookSpec(
                        type="command",
                        command=registration.command,
                        timeout=registration.timeout,
                        once=registration.once,
                        matcher=registration.matcher,
                        event=registration.event,
                    )
                )
            )
        return handlers

    def status_counts(self) -> dict[str, int]:
        """统计 enabled/disabled/failed 三类注册记录数量。"""
        counts = {"enabled": 0, "disabled": 0, "failed": 0}
        for registration in self.registrations:
            counts[registration.status] += 1
        return counts


def command_hooks_from_settings(settings: dict) -> list[CommandHookHandler]:
    """便捷函数：从 settings 直接得到可注册到 HookBus 的 handler 列表。"""
    return CommandRegistry.from_settings(settings).enabled_handlers()


def _registrations_from_settings(settings: dict) -> list[CommandRegistration]:
    """把 settings 里的 hooks 配置解析成注册记录列表。"""
    registrations: list[CommandRegistration] = []
    if not settings:
        return registrations
    if not isinstance(settings, dict):
        # settings.hooks 整体类型错了，也生成一条 failed registration，
        # 这样 doctor 能显示具体错误，而不是静默忽略。
        return [
            _registration(
                "settings",
                event="",
                matcher=None,
                command="",
                status="failed",
                reason="hooks settings must be an object",
            )
        ]
    for event, entries in settings.items():
        # 第一层 key 是 hook 事件名，例如 PreToolUse、ContextBuild。
        if event not in VALID_HOOK_EVENTS:
            registrations.append(_registration(f"settings:{event}", event=str(event), matcher=None, command="", status="failed", reason="invalid hook event"))
            continue
        if not isinstance(entries, list):
            registrations.append(_registration(f"settings:{event}", event=event, matcher=None, command="", status="failed", reason="event entries must be a list"))
            continue
        for entry_index, entry in enumerate(entries):
            # 每个 entry 可以设置 matcher/enabled，并包含 hooks 数组。
            if not isinstance(entry, dict):
                registrations.append(_registration(f"settings:{event}:{entry_index}", event=event, matcher=None, command="", status="failed", reason="hook entry must be an object"))
                continue
            matcher = entry.get("matcher") if isinstance(entry.get("matcher"), str) else None
            entry_enabled = bool(entry.get("enabled", True))
            hooks = entry.get("hooks") or []
            if not isinstance(hooks, list):
                registrations.append(_registration(f"settings:{event}:{entry_index}", event=event, matcher=matcher, command="", status="failed", reason="hooks must be a list"))
                continue
            for hook_index, raw in enumerate(hooks):
                reg_id = f"settings:{event}:{entry_index}:{hook_index}"
                registrations.append(_registration_from_raw(reg_id, event, matcher, raw, entry_enabled=entry_enabled))
    return registrations


def _registration_from_raw(reg_id: str, event: str, matcher: str | None, raw: object, *, entry_enabled: bool) -> CommandRegistration:
    """解析单条原始 hook 配置。"""
    if not isinstance(raw, dict):
        return _registration(reg_id, event=event, matcher=matcher, command="", status="failed", reason="hook must be an object")
    if raw.get("type") != "command":
        # 当前只支持 command hook。其它类型标记 disabled 而不是 failed，
        # 表示配置可能为未来阶段预留，但本版本不会执行。
        return _registration(reg_id, event=event, matcher=matcher, command="", status="disabled", reason="unsupported hook type")
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        return _registration(reg_id, event=event, matcher=matcher, command="", status="failed", reason="command hook requires a command string")
    timeout_raw = raw.get("timeout", 60)
    try:
        timeout = int(timeout_raw)
    except (TypeError, ValueError):
        return _registration(reg_id, event=event, matcher=matcher, command=command, status="failed", reason="timeout must be an integer")
    if timeout <= 0:
        return _registration(reg_id, event=event, matcher=matcher, command=command, status="failed", reason="timeout must be positive")

    # 外层 entry 和当前 hook 自己都启用时，最终才算 enabled。
    enabled = entry_enabled and bool(raw.get("enabled", True))
    return CommandRegistration(
        id=reg_id,
        event=event,
        matcher=matcher,
        command=command,
        timeout=timeout,
        once=bool(raw.get("once", False)),
        enabled=enabled,
        status="enabled" if enabled else "disabled",
        reason="" if enabled else "disabled by settings",
        source="settings",
    )


def _unsupported_plugin_commands(plugin_roots: list[Path]) -> list[CommandRegistration]:
    """扫描插件 manifest 中暂不支持的 commands 配置，并生成诊断记录。"""
    registrations: list[CommandRegistration] = []
    for root in plugin_roots:
        if not root.exists() or not root.is_dir():
            continue
        for manifest_path in sorted(root.glob("*/.codex-plugin/plugin.json")):
            manifest, error = read_json_file(manifest_path)
            if error or not manifest:
                continue
            plugin_name = manifest.get("name") if isinstance(manifest.get("name"), str) else manifest_path.parent.parent.name
            commands = manifest.get("commands") or []
            if not isinstance(commands, list):
                registrations.append(
                    _registration(
                        f"plugin:{plugin_name}:commands",
                        event="",
                        matcher=None,
                        command="",
                        status="failed",
                        reason="plugin commands must be a list",
                        source=f"plugin:{plugin_name}",
                    )
                )
                continue
            for index, raw in enumerate(commands):
                # 插件 command 在当前阶段不执行，但列出来能让用户知道：
                # manifest 里确实声明过 command，只是 BigCode 还没接入。
                command = raw.get("command") if isinstance(raw, dict) and isinstance(raw.get("command"), str) else ""
                registrations.append(
                    _registration(
                        f"plugin:{plugin_name}:{index}",
                        event=str(raw.get("event", "")) if isinstance(raw, dict) else "",
                        matcher=raw.get("matcher") if isinstance(raw, dict) and isinstance(raw.get("matcher"), str) else None,
                        command=command,
                        status="disabled",
                        reason="plugin command registration is not supported in Phase 3",
                        source=f"plugin:{plugin_name}",
                    )
                )
    return registrations


def _registration(
    reg_id: str,
    *,
    event: str,
    matcher: str | None,
    command: str,
    status: CommandRegistrationStatus,
    reason: str,
    source: str = "settings",
) -> CommandRegistration:
    """构造一条 CommandRegistration，主要用于失败/禁用场景的统一记录。"""
    return CommandRegistration(
        id=reg_id,
        event=event,
        matcher=matcher,
        command=command,
        timeout=60,
        once=False,
        enabled=status == "enabled",
        status=status,
        reason=reason,
        source=source,
    )


def _parse_last_json(text: str) -> dict | None:
    """从命令输出末尾解析最后一个 JSON 对象。"""
    text = text.strip()
    if not text:
        return None
    start = text.rfind("{")
    if start < 0:
        return None
    try:
        value = json.loads(text[start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
