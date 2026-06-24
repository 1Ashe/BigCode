"""BigCode 的命令行入口。

学习思路：main() 先解析参数和配置，再按 repl/run/resume/doctor 分支创建 AgentSession 或生成诊断报告。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from bigcode.agent import AgentSession, serialize_agent_event
from bigcode.agent.snapshot import SessionListItem, list_session_snapshots
from bigcode.config import load_runtime_config
from bigcode.diagnostics import build_doctor_report, render_doctor_report
from bigcode.ui import BigCodeStreamRenderer, BigCodeTUI
from bigcode.ui.repl import run_repl


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""
    parser = argparse.ArgumentParser(prog="bigcode", description="Interactive BigCode coding agent")
    parser.add_argument("command", nargs="?", default="repl", choices=["repl", "resume", "run", "doctor"], help="Command to run")
    parser.add_argument("arg", nargs="?", help="Prompt for run, or session id for resume")
    parser.add_argument("--cwd", default=".", help="Workspace cwd")
    parser.add_argument("--model", dest="model_ref", help="Model reference provider:model")
    parser.add_argument("--non-interactive", action="store_true", help="Disable prompts and convert permission asks to denial")
    parser.add_argument("--event-stream", choices=["off", "jsonl"], default="off", help="Emit machine-readable run events instead of plain text output")
    parser.add_argument("--no-probe", action="store_true", help="Do not make provider or MCP probe requests in doctor")
    parser.add_argument("--timeout", type=float, default=10.0, help="Provider probe timeout in seconds for doctor")
    return parser


def main(argv: list[str] | None = None) -> None:
    """命令行主入口。

    解析参数后加载 RuntimeConfig，再按 doctor/run/resume/repl 分支执行对应流程。
    """
    args = build_parser().parse_args(argv)
    config = load_runtime_config(Path(args.cwd))
    if args.command == "doctor":
        # doctor 只做诊断，不创建 AgentSession。这样模型配置坏了时也能输出报告。
        report = asyncio.run(
            build_doctor_report(
                config,
                model_ref=args.model_ref,
                probe=not args.no_probe,
                timeout=args.timeout,
            )
        )
        print(render_doctor_report(report), end="")
        if report.has_errors:
            raise SystemExit(1)
        return

    if args.command == "resume" and not args.arg:
        # 不带 session id 的 resume 用来列出可恢复会话。
        print(_render_resume_list(list_session_snapshots(config.project_state_dir)), end="")
        return

    if args.event_stream != "off" and args.command != "run":
        raise SystemExit("--event-stream is currently supported only with `bigcode run`.")

    session_id = args.arg if args.command == "resume" else None
    # repl、resume、run 最终都会创建 AgentSession；区别主要是是否带 session_id，
    # 以及 run 只执行一次 prompt，repl 会进入循环。
    session = AgentSession(config, session_id=session_id, model_ref=args.model_ref, non_interactive=args.non_interactive)
    if args.command == "run":
        if not args.arg:
            raise SystemExit("bigcode run requires a prompt argument")
        asyncio.run(_run_once(session, args.arg, event_stream=args.event_stream))
        return
    asyncio.run(run_repl(session))


async def _run_once(session: AgentSession, prompt: str, *, event_stream: str = "off"):
    """启动 session 并执行一次非交互式 prompt。"""
    try:
        await session.start()
        if event_stream == "jsonl":
            async for event in session.run_turn_stream(prompt):
                sys.stdout.write(json.dumps(serialize_agent_event(event), ensure_ascii=False, sort_keys=True))
                sys.stdout.write("\n")
                sys.stdout.flush()
            return None
        ui = BigCodeTUI(enabled=True)
        renderer = BigCodeStreamRenderer(ui)
        try:
            async for event in session.run_turn_stream(prompt):
                renderer.handle(event)
        finally:
            renderer.close()
        return None
    finally:
        await session.shutdown()


def _render_resume_list(items: list[SessionListItem]) -> str:
    """把可恢复会话列表渲染成制表符分隔文本。"""
    if not items:
        return "No resumable sessions.\n"
    lines = ["id\tupdated_at\tmodel\tmessages\tcwd"]
    for item in items:
        lines.append(
            "\t".join(
                [
                    item.session_id,
                    _format_time(item.updated_at),
                    item.model or "",
                    str(item.message_count),
                    item.cwd,
                ]
            )
        )
    return "\n".join(lines) + "\n"


def _format_time(timestamp: float) -> str:
    """把 Unix 时间戳格式化成本地时间字符串。"""
    if not timestamp:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


if __name__ == "__main__":
    main()
