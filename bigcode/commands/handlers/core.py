"""Built-in local slash command handlers."""
from __future__ import annotations

import shlex
from typing import Any

from bigcode.context.compact import CompactDeps, apply_context_compact
from bigcode.diagnostics import build_doctor_report, render_doctor_report
from bigcode.tools.base import EmptyInput
from bigcode.tools.plan.EnterPlanMode import EnterPlanModeTool

from ..registry import Command, CommandContext, CommandType


async def handle_exit(ctx: CommandContext) -> bool:
    return True


async def handle_help(ctx: CommandContext) -> bool:
    registry = ctx.repl.command_registry if ctx.repl is not None else None
    if registry is None:
        ctx.ui.print("Command registry is unavailable.")
        return False
    if ctx.args:
        command = registry.find(ctx.args)
        if command is None:
            ctx.ui.print(f"Unknown command: /{ctx.args}")
            return False
        lines = [f"/{command.name}"]
        if command.aliases:
            lines[0] += f"  (aliases: {', '.join('/' + alias for alias in command.aliases)})"
        lines.append(f"  {command.description}")
        if command.usage:
            lines.append(f"  usage: {command.usage}")
        if command.arg_prompt:
            lines.append(f"  args: {command.arg_prompt}")
        ctx.ui.print("\n".join(lines))
        return False

    lines = ["Commands:"]
    for command in registry.list_commands():
        names = "/" + command.name
        if command.aliases:
            names += ", " + ", ".join("/" + alias for alias in command.aliases)
        lines.append(f"  {names:<28} {command.description}")
    lines.append("")
    lines.append("Use /help <command> for details.")
    ctx.ui.print("\n".join(lines))
    return False


async def handle_clear(ctx: CommandContext) -> bool:
    ctx.ui.print("\033[2J\033[H", end="")
    return False


async def handle_doctor(ctx: CommandContext) -> bool:
    parts = _split_args(ctx.args)
    report = await build_doctor_report(
        ctx.session.config,
        model_ref=ctx.session.model_ref,
        probe="--no-probe" not in parts,
        timeout=_parse_timeout(parts),
        registry=ctx.session.registry,
        skill_registry=ctx.session.skill_registry,
        mcp_manager=ctx.session.mcp_manager,
    )
    ctx.ui.print(render_doctor_report(report), end="")
    return False


async def handle_status(ctx: CommandContext) -> bool:
    if ctx.repl is not None:
        ctx.ui.status_table(ctx.repl.status_rows())
        return False
    ctx.ui.status_table(_status_rows(ctx.session))
    return False


async def handle_plan(ctx: CommandContext) -> bool:
    if not ctx.session.plan_state.active:
        await EnterPlanModeTool().call(EmptyInput(), ctx.session.make_tool_context())
        ctx.ui.print(f"Entered Plan Mode: {ctx.session.plan_state.plan_file}")
    else:
        content = ctx.session.plan_store.read(ctx.session.session_id) or ""
        ctx.ui.print(f"Plan file: {ctx.session.plan_state.plan_file}")
        ctx.ui.print(content or "(empty)")
    if ctx.args:
        if ctx.repl is None:
            ctx.ui.print("Cannot submit /plan prompt without an active REPL.")
        else:
            await ctx.repl.run_turn(ctx.args, allow_escape_cancel=True)
    return False


async def handle_compact(ctx: CommandContext) -> bool:
    compacted = await apply_context_compact(
        ctx.session.messages,
        CompactDeps(
            config=ctx.session.config.compact,
            state=ctx.session.compact_state,
            context_window=ctx.session.model.context_window or 128000,
            system_prompt=ctx.session.system_prompt,
            tool_schemas=ctx.session.registry.schemas_for_model(),
            is_main_thread=ctx.session.is_main_thread,
            summarize=ctx.session._summarize_context,
        ),
        force_auto=True,
    )
    ctx.session._append_compact_records(compacted.records_to_append)
    ctx.session.read_file_state.clear()
    ctx.session._save_snapshot()
    ctx.ui.print(
        "Compacted context: "
        f"{compacted.tokens_before} -> {compacted.tokens_after} tokens "
        f"({compacted.utilization_after:.1%})"
    )
    return False


async def handle_session(ctx: CommandContext) -> bool:
    transcript = getattr(ctx.session, "transcript", None)
    rows = {
        "session": ctx.session.session_id,
        "model": ctx.session.model_ref,
        "cwd": ctx.session.config.cwd,
        "messages": len(ctx.session.messages),
        "transcript": getattr(transcript, "path", None) or getattr(transcript, "file_path", None) or "(unknown)",
        "snapshot": "enabled" if ctx.session.persist_snapshot else "disabled",
    }
    ctx.ui.status_table(rows)
    return False


async def handle_mcp(ctx: CommandContext) -> bool:
    manager = ctx.session.mcp_manager
    if ctx.args.strip() == "discover":
        await manager.discover()
    rows = {
        "enabled": manager.enabled,
        "fastmcp available": manager.fastmcp_available,
        "configured servers": len(manager.servers),
        "discovered capabilities": len(manager.capabilities),
    }
    ctx.ui.status_table(rows)
    for name, server in sorted(manager.servers.items()):
        state = "enabled" if server.enabled else "disabled"
        desc = f" - {server.description}" if server.description else ""
        ctx.ui.print(f"  {name}: {state}{desc}")
    return False


async def handle_skill(ctx: CommandContext) -> bool:
    parts = _split_args(ctx.args)
    registry = ctx.session.skill_registry
    if not parts or parts[0] == "list":
        skills = sorted(registry.list(), key=lambda skill: skill.name)
        if not skills:
            ctx.ui.print("No skills loaded.")
            return False
        for skill in skills:
            ctx.ui.print(f"{skill.name}: {skill.description or '(no description)'}")
        counts = registry.status_counts()
        ctx.ui.print(f"enabled {counts['enabled']}, disabled {counts['disabled']}, failed {counts['failed']}")
        return False
    if parts[0] == "info" and len(parts) >= 2:
        skill = registry.get(parts[1])
        if skill is None:
            ctx.ui.print(f"Unknown skill: {parts[1]}")
            return False
        rows = {
            "name": skill.name,
            "description": skill.description or "(none)",
            "source": skill.source,
            "path": skill.skill_md,
            "resources": len(skill.resources),
        }
        if skill.plugin_name:
            rows["plugin"] = skill.plugin_name
        ctx.ui.status_table(rows)
        return False
    ctx.ui.print("Usage: /skill [list|info <name>]")
    return False


async def handle_permission(ctx: CommandContext) -> bool:
    parts = _split_args(ctx.args)
    valid_modes = {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk"}
    if not parts:
        ctx.ui.print(f"permission mode: {ctx.session.permission_context.mode}")
        return False
    mode = parts[0]
    if mode not in valid_modes:
        ctx.ui.print(f"Unknown permission mode: {mode}")
        ctx.ui.print("Valid modes: " + ", ".join(sorted(valid_modes)))
        return False
    previous = ctx.session.permission_context.mode
    ctx.session.permission_context.mode = mode
    ctx.session._save_snapshot()
    ctx.ui.print(f"permission mode: {previous} -> {mode}")
    return False


async def handle_memory(ctx: CommandContext) -> bool:
    manager = ctx.session.memory_manager
    if manager is None:
        ctx.ui.print("Memory is unavailable in this session.")
        return False
    content = manager.load_index_for_prompt()
    if not content:
        ctx.ui.print("No memories found.")
        return False
    ctx.ui.print(content)
    return False


def _status_rows(session: Any) -> dict[str, Any]:
    rows: dict[str, Any] = {
        "session": session.session_id,
        "cwd": session.config.cwd,
        "model": session.model_ref,
        "protocol": session.model_protocol_label(),
        "permission mode": session.permission_context.mode,
        "messages": len(session.messages),
        "loaded skills": ", ".join(sorted(session.loaded_skills)) if session.loaded_skills else "(none)",
    }
    if session.last_verification:
        rows["last verification"] = f"{session.last_verification.get('command')} (exit {session.last_verification.get('exit_code')})"
    counts = session.agent_task_store.status_counts()
    total_background = sum(counts.values())
    rows["background subagents"] = (
        f"{total_background} "
        f"(queued {counts.get('queued', 0)}, running {counts.get('running', 0)}, "
        f"completed {counts.get('completed', 0)}, failed {counts.get('failed', 0)}, "
        f"cancelled {counts.get('cancelled', 0)})"
    )
    rows["fastmcp available"] = session.mcp_manager.fastmcp_available
    return rows


def _split_args(args: str) -> list[str]:
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


def _parse_timeout(parts: list[str]) -> float:
    if "--timeout" not in parts:
        return 10.0
    idx = parts.index("--timeout")
    try:
        return float(parts[idx + 1])
    except (IndexError, ValueError):
        return 10.0


EXIT_COMMAND = Command(
    name="exit",
    aliases=["quit", "q"],
    description="Exit the REPL",
    usage="/exit",
    type=CommandType.EXIT,
    handler=handle_exit,
)

HELP_COMMAND = Command(
    name="help",
    aliases=["h", "?"],
    description="Show slash command help",
    usage="/help [command]",
    type=CommandType.LOCAL,
    handler=handle_help,
)

CLEAR_COMMAND = Command(
    name="clear",
    aliases=["cls"],
    description="Clear the terminal screen",
    usage="/clear",
    type=CommandType.LOCAL_UI,
    handler=handle_clear,
)

DOCTOR_COMMAND = Command(
    name="doctor",
    aliases=["diag"],
    description="Run configuration and environment diagnostics",
    usage="/doctor [--no-probe] [--timeout seconds]",
    type=CommandType.LOCAL,
    handler=handle_doctor,
)

STATUS_COMMAND = Command(
    name="status",
    aliases=["s"],
    description="Show current session status",
    usage="/status",
    type=CommandType.LOCAL,
    handler=handle_status,
)

PLAN_COMMAND = Command(
    name="plan",
    aliases=["p"],
    description="Enter Plan Mode or show the active plan",
    usage="/plan [prompt]",
    type=CommandType.LOCAL_UI,
    handler=handle_plan,
)

COMPACT_COMMAND = Command(
    name="compact",
    aliases=["c"],
    description="Compact conversation context",
    usage="/compact",
    type=CommandType.LOCAL,
    handler=handle_compact,
)

SESSION_COMMAND = Command(
    name="session",
    aliases=["sess"],
    description="Show session details",
    usage="/session",
    type=CommandType.LOCAL,
    handler=handle_session,
)

MCP_COMMAND = Command(
    name="mcp",
    description="Show MCP server status",
    usage="/mcp [discover]",
    type=CommandType.LOCAL,
    handler=handle_mcp,
)

SKILL_COMMAND = Command(
    name="skill",
    aliases=["skills"],
    description="List skills or show skill details",
    usage="/skill [list|info <name>]",
    type=CommandType.LOCAL,
    handler=handle_skill,
)

PERMISSION_COMMAND = Command(
    name="permission",
    aliases=["perm"],
    description="Show or set the current permission mode",
    usage="/permission [mode]",
    arg_prompt="mode is one of default, acceptEdits, plan, bypassPermissions, dontAsk",
    type=CommandType.LOCAL,
    handler=handle_permission,
)

MEMORY_COMMAND = Command(
    name="memory",
    aliases=["mem"],
    description="Show loaded memory index",
    usage="/memory",
    type=CommandType.LOCAL,
    handler=handle_memory,
)

ALL_COMMANDS = [
    HELP_COMMAND,
    EXIT_COMMAND,
    CLEAR_COMMAND,
    DOCTOR_COMMAND,
    STATUS_COMMAND,
    PLAN_COMMAND,
    COMPACT_COMMAND,
    SESSION_COMMAND,
    MCP_COMMAND,
    SKILL_COMMAND,
    PERMISSION_COMMAND,
    MEMORY_COMMAND,
]
