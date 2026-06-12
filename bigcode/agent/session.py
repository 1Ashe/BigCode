"""AgentSession 是 BigCode 的主控制器。

学习思路：一次用户提问会进入 run_turn()，它负责构造上下文、请求模型、执行工具、保存 transcript/snapshot，并在需要时继续下一轮模型调用。
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bigcode.agent.events import EventSink, StatusEvent, TurnCompleted
from bigcode.agent.snapshot import SessionSnapshot, load_session_snapshot, save_session_snapshot
from bigcode.config.models import ResolvedModel, RuntimeConfig
from bigcode.context.builder import ContextBuildDeps, build_context_for_api
from bigcode.context.messages import AssistantMessage, MessageBase, TextBlock, ToolUseBlock, UserMessage, text_from_blocks
from bigcode.context.normalizer import tool_run_result_to_message
from bigcode.context.transcript import Transcript
from bigcode.hooks.builtins import register_builtin_hooks
from bigcode.hooks.models import HookInput
from bigcode.hooks import HookBus
from bigcode.mcp import McpClientManager
from bigcode.models import ClaudeCompatibleModelClient
from bigcode.plan import PlanModeState, PlanStore
from bigcode.skills import load_skills
from bigcode.subagents.definitions import AgentDefinition
from bigcode.subagents.tasks import AgentRunResult, AgentTaskState, AgentTaskStore, render_agent_result, result_to_dict
from bigcode.tasks import TaskStore
from bigcode.tools.artifacts.ArtifactRead import ArtifactRecord, ArtifactStore
from bigcode.tools import ToolExecutionContext, ToolRegistry, ToolRunner, ToolUse, build_default_registry
from bigcode.tools.permissions import ToolPermissionContext, PermissionRule
from bigcode.tools.read_file_state import ReadFileState
from bigcode.utils.ids import new_id


@dataclass
class AgentTurnResult:
    """单轮对话的返回值。

    assistant_text 是最终回复文本；tool_results 保存本轮所有工具执行结果；stop_reason 记录模型或流程停止原因。
    """
    assistant_text: str
    tool_results: list[Any] = field(default_factory=list)
    stop_reason: str | None = None


class AgentSession:
    """一次 BigCode 会话的完整状态和主流程。

    它把配置、消息历史、工具注册表、hook、技能、MCP、任务和快照都串起来，是阅读本项目最重要的类。
    """
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        session_id: str | None = None,
        model_ref: str | None = None,
        registry: ToolRegistry | None = None,
        non_interactive: bool = False,
        event_sink: EventSink | None = None,
        transcript_path: Path | None = None,
        persist_snapshot: bool = True,
        load_transcript: bool = True,
        abort_event: threading.Event | None = None,
    ) -> None:
        """初始化会话状态。

        这里会尝试加载已有快照，创建工具运行器、hook bus、技能注册表、MCP 管理器、transcript 和 artifact store。
        """
        self.config = config
        self.persist_snapshot = persist_snapshot

        # session_id 传进来通常表示“恢复旧会话”；能读到 snapshot 时优先复用旧状态，
        # 读不到时就按新会话处理。这样 resume 和新建会话走同一个初始化路径。
        snapshot = load_session_snapshot(config.project_state_dir, session_id) if session_id and persist_snapshot else None
        self.session_id = session_id or new_id("session")
        self.task_list_id = (snapshot.task_list_id if snapshot else None) or config.task_default_list_id or self.session_id
        self.model_ref = model_ref or (snapshot.model if snapshot else None) or config.default_model_ref

        # 权限上下文会在 Plan Mode、resume 等流程中被修改，所以这里必须复制一份，
        # 不能直接拿 config.permission_context 原对象来改。
        self.permission_context = _clone_permission_context(config.permission_context)
        if snapshot and snapshot.permission_mode in {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"}:
            self.permission_context.mode = snapshot.permission_mode

        # registry 保存工具定义，runner 负责真正执行工具；二者分开后，
        # 子代理可以拿到“裁剪过的 registry”，但仍复用同一套执行逻辑。
        self.registry = registry or build_default_registry()
        self.runner = ToolRunner(self.registry)
        self.messages: list[MessageBase] = []

        # 下面这些字段是 resume 时最需要恢复的会话状态：
        # 文件快照保护编辑安全，技能/artifact/验证命令则影响后续上下文提醒。
        self.read_file_state = ReadFileState.from_snapshot(snapshot.read_file_snapshots) if snapshot else ReadFileState()
        self.loaded_skills: set[str] = set(snapshot.loaded_skills if snapshot else [])
        self.active_artifacts: dict[str, dict[str, Any]] = dict(snapshot.active_artifacts if snapshot else {})
        self.last_verification: dict[str, Any] | None = snapshot.last_verification if snapshot else None
        self.abort_event = abort_event or threading.Event()
        self.plan_state = PlanModeState()
        self.task_store = TaskStore(config.cwd / ".bigcode" / "tasks")
        self.plan_store = PlanStore(config.plan_default_dir)
        self.agent_task_store = AgentTaskStore(config.project_state_dir)
        self._background_subagent_runs: dict[str, asyncio.Task[None]] = {}
        self.skill_registry = load_skills(config.skill_roots)
        self.mcp_manager = McpClientManager(config.mcp_servers, enabled=config.mcp_enabled)
        self.non_interactive = non_interactive
        self.event_sink = event_sink
        self.hook_bus = HookBus()
        register_builtin_hooks(self.hook_bus)

        # transcript 是完整消息流水账，snapshot 是可快速恢复的状态摘要。
        # 两者都保存，是为了兼顾“能恢复完整历史”和“能快速列出/恢复会话”。
        self.transcript = Transcript(transcript_path or config.project_state_dir / "transcripts" / f"{self.session_id}.jsonl")
        self.artifact_store = ArtifactStore(config.project_state_dir, self.session_id)
        if session_id and load_transcript:
            self.messages = self.transcript.load()

    @property
    def model(self) -> ResolvedModel:
        """根据 model_ref 从配置表中取出当前模型。

        如果没有默认模型或模型名不存在，会提前报错，避免真正请求模型时才失败。
        """
        if not self.model_ref:
            raise RuntimeError("No default model configured. Add .bigcode/models.json and default_model.")
        model = self.config.models.get(self.model_ref)
        if not model:
            raise RuntimeError(f"Model {self.model_ref!r} is not configured.")
        return model

    def make_tool_context(self) -> ToolExecutionContext:
        """构造工具执行上下文。

        ToolRunner 不直接依赖 AgentSession，而是通过这个轻量对象拿 cwd、权限、任务、技能、MCP 等能力。
        """
        # 这里没有复制状态对象，而是把同一批状态引用传给工具。
        # 所以工具更新 read_file_state、plan_state、active_artifacts 时，AgentSession 能立刻看到。
        return ToolExecutionContext(
            cwd=self.config.cwd,
            workspace_roots=self.config.workspace_roots,
            permission_context=self.permission_context,
            read_file_state=self.read_file_state,
            abort_event=self.abort_event,
            session_id=self.session_id,
            hook_bus=self.hook_bus,
            is_non_interactive_session=self.non_interactive,
            sandbox_profile=self.config.sandbox_profile,
            plan_state=self.plan_state,
            task_store=self.task_store,
            plan_store=self.plan_store,
            skill_registry=self.skill_registry,
            mcp_manager=self.mcp_manager,
            agent_session=self,
            event_sink=self.event_sink,
            artifact_store=self.artifact_store,
            active_artifacts=self.active_artifacts,
            project_state_dir=self.config.project_state_dir,
        )

    async def start(self) -> None:
        """启动会话时的初始化动作。

        它发送 session_started 事件，触发 SessionStart hook，并保存一次快照。
        """
        self._emit_status("session_started", model=self.model_ref, cwd=str(self.config.cwd))
        await self.hook_bus.emit("SessionStart", HookInput("SessionStart", self.session_id, str(self.config.cwd), self.permission_context.mode))
        self._save_snapshot()

    async def run_turn(self, prompt: str, *, max_steps: int = 20) -> AgentTurnResult:
        """执行一次用户输入到模型回复的完整循环。

        一次 turn 可能包含多步：模型先回复工具调用，工具执行结果再回填给模型，直到模型输出最终文本或达到 max_steps。
        """
        self._emit_status("turn_started", max_steps=max_steps)
        user_msg = UserMessage(prompt)
        self.messages.append(user_msg)
        self._append_transcript(user_msg)
        await self.hook_bus.emit(
            "UserPromptSubmit",
            HookInput("UserPromptSubmit", self.session_id, str(self.config.cwd), self.permission_context.mode, payload={"prompt": prompt}),
        )
        turn_tool_names: list[str] = []
        assistant_text = ""
        tool_results: list[Any] = []
        for step in range(max_steps):
            self._emit_status("model_request_started", step=step)

            # 每一步请求模型前都重新构建上下文，因为上一轮工具结果、hook 附件、
            # Plan Mode 状态等都可能刚刚变化。
            built = await build_context_for_api(
                self.messages,
                ContextBuildDeps(
                    session_id=self.session_id,
                    cwd=self.config.cwd,
                    instruction_paths=self.config.instruction_paths,
                    tool_names=[tool.name for tool in self.registry.list_tools()],
                    hook_bus=self.hook_bus,
                    permission_mode=self.permission_context.mode,
                    plan_mode_state=self.plan_state,
                    task_store=self.task_store,
                    task_list_id=self.task_list_id,
                    capabilities=self._capabilities(),
                ),
            )
            response = await ClaudeCompatibleModelClient(self.model).complete(
                built.system_prompt,
                built.api_messages,
                self.registry.schemas_for_model(),
            )
            self._emit_status("model_response_completed", step=step)
            assistant = response.message
            self.messages.append(assistant)
            self._append_transcript(assistant)
            text = text_from_blocks(assistant.content)
            if text:
                assistant_text = text
            tool_uses = [ToolUse(block.id, block.name, block.input) for block in assistant.content if isinstance(block, ToolUseBlock)]
            if not tool_uses:
                # 没有工具调用通常表示模型准备结束本轮；Stop hook 仍有最后机会
                # 要求继续，例如 Plan Mode 必须用 ExitPlanMode/AskUserQuestion 收尾。
                stop = await self.hook_bus.emit(
                    "Stop",
                    HookInput(
                        "Stop",
                        self.session_id,
                        str(self.config.cwd),
                        self.permission_context.mode,
                        payload={"turn_tool_names": turn_tool_names, "plan_mode_state": self.plan_state, "last_assistant_text": assistant_text},
                    ),
                )
                if stop.continue_turn and step + 1 < max_steps:
                    reminder = UserMessage(stop.reason or "Continue.", is_meta=True, origin="hooks")
                    self.messages.append(reminder)
                    self._append_transcript(reminder)
                    continue
                self._emit_status("turn_completed", stop_reason=assistant.stop_reason)
                self._emit_turn_completed(assistant_text, assistant.stop_reason, len(tool_results))
                return AgentTurnResult(assistant_text=assistant_text, tool_results=tool_results, stop_reason=assistant.stop_reason)
            turn_tool_names.extend(t.name for t in tool_uses)

            # 模型请求工具时，不直接把结果返回给用户；先执行工具，再把 tool_result
            # 作为新的用户侧 meta 消息塞回 messages，让模型基于结果继续推理。
            results = await self.runner.run_tool_uses(tool_uses, self.make_tool_context())
            tool_results.extend(results)
            for result in results:
                result_msg = tool_run_result_to_message(result)
                self.messages.append(result_msg)
                self._append_transcript(result_msg)
        self._emit_status("turn_max_steps", max_steps=max_steps)
        self._emit_turn_completed(assistant_text, "max_steps", len(tool_results))
        return AgentTurnResult(assistant_text=assistant_text, tool_results=tool_results, stop_reason="max_steps")

    async def run_subagent(
        self,
        definition: AgentDefinition,
        prompt: str,
        *,
        model_ref: str | None = None,
        agent_id: str | None = None,
        is_background: bool = False,
        sidechain_transcript_path: Path | None = None,
        abort_event: threading.Event | None = None,
    ) -> AgentRunResult:
        """创建一个子 AgentSession 并同步运行。

        子代理复用父会话的部分状态，但拥有独立 transcript；结束后会把子代理写过的文件快照合并回父会话。
        """
        started = time.perf_counter()
        agent_id = agent_id or new_id(f"subagent_{definition.name}")
        sidechain_transcript_path = sidechain_transcript_path or self.agent_task_store.transcript_path(agent_id)
        child_abort_event = abort_event or (threading.Event() if is_background else self.abort_event)

        # 子代理也是一个完整 AgentSession，只是它不保存主 session snapshot，
        # 并使用 sidechain transcript，避免污染父会话的主 transcript。
        child = AgentSession(
            self.config,
            session_id=agent_id,
            model_ref=model_ref or definition.model or self.model_ref,
            registry=_registry_for_subagent(self.registry, definition, is_background=is_background),
            non_interactive=True,
            event_sink=self.event_sink,
            transcript_path=sidechain_transcript_path,
            persist_snapshot=False,
            load_transcript=False,
            abort_event=child_abort_event,
        )
        child.permission_context.mode = _resolve_subagent_permission_mode(self.permission_context.mode, definition)
        child.task_list_id = self.task_list_id
        child.task_store = self.task_store
        child.plan_store = self.plan_store
        child.skill_registry = self.skill_registry
        child.mcp_manager = self.mcp_manager
        child.hook_bus = self.hook_bus
        child.read_file_state = self.read_file_state.clone()

        # 上面先复用/覆盖父会话的共享组件，再触发 hook；这样 hook 看到的是
        # 子代理实际运行时会使用的 task_store、skill_registry 和权限模式。
        await self._emit_subagent_hook(
            "SubagentStart",
            child,
            definition,
            agent_id,
            sidechain_transcript_path,
            is_background=is_background,
        )
        result_summary = ""
        stop_reason = None
        error: str | None = None
        try:
            result = await child.run_turn(f"{definition.system_prompt}\n\nTask:\n{prompt}", max_steps=definition.max_turns)
            result_summary = result.assistant_text
            stop_reason = result.stop_reason
            run_result = AgentRunResult(
                agent_id=agent_id,
                agent_type=definition.name,
                content=result.assistant_text,
                total_tool_use_count=len(result.tool_results),
                total_duration_ms=int((time.perf_counter() - started) * 1000),
                total_tokens=_total_tokens(child.messages),
                stop_reason=result.stop_reason,
                sidechain_transcript_path=str(sidechain_transcript_path),
            )
            self._merge_child_written_snapshots(child)
            return run_result
        except asyncio.CancelledError:
            error = "cancelled"
            stop_reason = "cancelled"
            raise
        except Exception as exc:
            error = str(exc).strip() or exc.__class__.__name__
            stop_reason = "error"
            raise
        finally:
            await self._emit_subagent_hook(
                "SubagentStop",
                child,
                definition,
                agent_id,
                sidechain_transcript_path,
                is_background=is_background,
                result_summary=result_summary,
                stop_reason=stop_reason,
                error=error,
            )

    def start_background_subagent(
        self,
        definition: AgentDefinition,
        prompt: str,
        *,
        description: str,
        model_ref: str | None = None,
    ) -> AgentTaskState:
        """启动后台子代理并立即返回任务状态。

        真正的子代理运行被包装成 asyncio task，结果会写入 AgentTaskStore。
        """
        agent_id = new_id(f"subagent_{definition.name}")
        state = self.agent_task_store.create(
            agent_id=agent_id,
            agent_type=definition.name,
            description=description,
            prompt=prompt,
            parent_session_id=self.session_id,
        )
        task = asyncio.create_task(self._run_background_subagent(state, definition, prompt, model_ref=model_ref))
        self._background_subagent_runs[agent_id] = task
        task.add_done_callback(lambda _task, _agent_id=agent_id: self._background_subagent_runs.pop(_agent_id, None))
        return state

    async def _run_background_subagent(
        self,
        state: AgentTaskState,
        definition: AgentDefinition,
        prompt: str,
        *,
        model_ref: str | None = None,
    ) -> None:
        """后台子代理 task 的执行体。

        它负责把任务状态从 queued 改成 running/completed/failed/cancelled，并持久化输出文件。
        """
        started = time.perf_counter()

        # 后台任务启动后先落盘 running 状态；即使进程随后崩溃，用户也能看到它曾经开始过。
        state.status = "running"
        state.started_at = time.time()
        self.agent_task_store.write_state(state)
        try:
            # 后台子代理拥有独立 abort_event，不会被父会话当前前台 turn 的取消信号误伤。
            result = await self.run_subagent(
                definition,
                prompt,
                model_ref=model_ref,
                agent_id=state.agent_id,
                is_background=True,
                sidechain_transcript_path=Path(state.sidechain_transcript_path),
                abort_event=threading.Event(),
            )

            # 正常完成后，把统计信息和结构化结果都写回 state，再单独写一份人类可读输出。
            state.status = "completed"
            state.completed_at = time.time()
            state.duration_ms = result.total_duration_ms
            state.total_tool_use_count = result.total_tool_use_count
            state.total_tokens = result.total_tokens
            state.stop_reason = result.stop_reason
            state.result = result_to_dict(result)
            self.agent_task_store.write_output(state.agent_id, render_agent_result(result))
        except asyncio.CancelledError:
            # CancelledError 不是普通 Exception 分支，必须单独捕获，
            # 否则 finally 会写状态，但输出文件不会说明用户取消了任务。
            state.status = "cancelled"
            state.completed_at = time.time()
            state.duration_ms = int((time.perf_counter() - started) * 1000)
            state.stop_reason = "cancelled"
            self.agent_task_store.write_output(state.agent_id, "[Subagent cancelled]\n")
        except Exception as exc:
            # 后台任务失败时不能把异常抛到前台；记录 failed 状态和错误文本即可。
            state.status = "failed"
            state.completed_at = time.time()
            state.duration_ms = int((time.perf_counter() - started) * 1000)
            state.error = str(exc).strip() or exc.__class__.__name__
            state.stop_reason = "error"
            self.agent_task_store.write_output(state.agent_id, f"[Subagent failed]\n{state.error}\n")
        finally:
            # finally 保证 running/completed/failed/cancelled 的最终状态都会落盘。
            self.agent_task_store.write_state(state)

    async def cancel_background_subagent(self, agent_id: str) -> dict[str, Any]:
        """尝试取消仍在运行的后台子代理。

        如果 asyncio task 还活着就 cancel；如果只剩历史状态，就返回 not_running。
        """
        task = self._background_subagent_runs.get(agent_id)
        state = self.agent_task_store.read_state(agent_id)
        if task and not task.done():
            task.cancel()
            cancelled_before_runner = False
            try:
                # shield 防止 wait_for 超时时继续取消 task；我们只是等它最多 1 秒写好状态。
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except asyncio.CancelledError:
                cancelled_before_runner = True
            except asyncio.TimeoutError:
                pass
            latest = self.agent_task_store.read_state(agent_id) or state
            if cancelled_before_runner and latest and latest.status != "cancelled":
                # 如果任务在进入 _run_background_subagent 前就被取消，那里来不及写 cancelled；
                # 这里补一次状态，避免用户看到任务永远停在 queued/running。
                latest.status = "cancelled"
                latest.completed_at = time.time()
                latest.stop_reason = "cancelled"
                self.agent_task_store.write_output(latest.agent_id, "[Subagent cancelled]\n")
                self.agent_task_store.write_state(latest)
            return {"status": "cancelled" if latest and latest.status == "cancelled" else "cancelling", "task": latest}
        if state:
            return {"status": "not_running", "task": state}
        raise RuntimeError(f"Unknown background subAgent task: {agent_id}")

    async def run_repl(self) -> None:
        """交互式命令行循环。

        TTY 模式会不断 input；管道模式会逐行读取 stdin，支持普通提问和 / 开头的本地命令。
        """
        await self.start()
        print(f"BigCode session {self.session_id}")
        if self.config.config_errors:
            # 配置加载阶段收集到的 warning 在启动时先打印出来，
            # 但不阻止用户继续进入 REPL。
            print("Config warnings:")
            for err in self.config.config_errors:
                print(f"  - {err}")
        if not self.model_ref:
            print("No model configured. Add .bigcode/models.json with default_model before asking model-backed questions.")
        if not sys.stdin.isatty():
            # 非 TTY 通常来自管道，例如 echo "..." | bigcode。
            # 这种模式不能持续交互，只能逐行处理 stdin。
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("/"):
                    should_exit = await self.handle_command(line)
                    if should_exit:
                        break
                    continue
                try:
                    result = await self.run_turn(line)
                except Exception as exc:
                    print(f"Error: {_format_exception(exc)}")
                    continue
                if result.assistant_text:
                    print(result.assistant_text)
            return
        while True:
            try:
                # input() 是阻塞函数；放进 asyncio.to_thread 可以避免堵住事件循环。
                line = await asyncio.to_thread(input, "\nbigcode> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                should_exit = await self.handle_command(line)
                if should_exit:
                    break
                continue
            try:
                result = await self.run_turn(line)
            except Exception as exc:
                print(f"Error: {_format_exception(exc)}")
                continue
            if result.assistant_text:
                print(result.assistant_text)

    async def handle_command(self, line: str) -> bool:
        """处理 /help、/doctor、/status、/plan、/compact 等本地命令。

        这些命令不走模型，直接读写当前会话状态或打印诊断信息。
        """
        cmd, _, arg = line.partition(" ")
        if cmd in {"/exit", "/quit"}:
            # 返回 True 表示 REPL 外层循环应该退出。
            return True
        if cmd == "/help":
            print("Commands: /help, /exit, /status, /doctor, /plan, /compact")
            return False
        if cmd == "/doctor":
            from bigcode.diagnostics import build_doctor_report, render_doctor_report

            parts = arg.split()
            probe = "--no-probe" not in parts
            timeout = _parse_timeout(parts)
            # 这里传入当前 session 已经构建好的 registry/skill_registry/mcp_manager，
            # 避免 doctor 再重复扫描一遍，也能反映当前会话真实状态。
            report = await build_doctor_report(
                self.config,
                model_ref=self.model_ref,
                probe=probe,
                timeout=timeout,
                registry=self.registry,
                skill_registry=self.skill_registry,
                mcp_manager=self.mcp_manager,
            )
            print(render_doctor_report(report), end="")
            return False
        if cmd == "/status":
            # /status 只读当前内存状态，适合排查“当前会话到底记住了什么”。
            print(f"session: {self.session_id}")
            print(f"cwd: {self.config.cwd}")
            print(f"model: {self.model_ref}")
            print(f"permission mode: {self.permission_context.mode}")
            print(f"sandbox profile: {self.config.sandbox_profile}")
            print(f"messages: {len(self.messages)}")
            print(f"loaded skills: {', '.join(sorted(self.loaded_skills)) if self.loaded_skills else '(none)'}")
            print(f"artifacts: {len(self.active_artifacts)}")
            if self.last_verification:
                print(f"last verification: {self.last_verification.get('command')} (exit {self.last_verification.get('exit_code')})")
            counts = self.agent_task_store.status_counts()
            total_background = sum(counts.values())
            print(
                "background subagents: "
                f"{total_background} "
                f"(queued {counts.get('queued', 0)}, running {counts.get('running', 0)}, "
                f"completed {counts.get('completed', 0)}, failed {counts.get('failed', 0)}, "
                f"cancelled {counts.get('cancelled', 0)})"
            )
            print(f"fastmcp available: {self.mcp_manager.fastmcp_available}")
            return False
        if cmd == "/plan":
            if not self.plan_state.active:
                from bigcode.tools.plan.EnterPlanMode import EnterPlanModeTool
                from bigcode.tools.base import EmptyInput

                # 本地命令直接调用工具类，复用工具内部对 PlanModeState 的更新逻辑。
                await EnterPlanModeTool().call(EmptyInput(), self.make_tool_context())
                print(f"Entered Plan Mode: {self.plan_state.plan_file}")
            else:
                # 已经在 Plan Mode 时，/plan 只展示当前计划文件，不会退出计划模式。
                content = self.plan_store.read(self.session_id) or ""
                print(f"Plan file: {self.plan_state.plan_file}")
                print(content or "(empty)")
            return False
        if cmd == "/compact":
            from bigcode.context.compact import apply_context_compact

            # 手动压缩只改变当前内存里的 messages；之后 append transcript 时会继续记录新消息。
            compacted = await apply_context_compact(self.messages, max_messages=40)
            self.messages = compacted.projected_messages
            print(f"Compacted messages: {len(self.messages)}")
            return False
        print(f"Unknown command: {cmd}")
        return False

    async def _emit_subagent_hook(
        self,
        event: str,
        child: "AgentSession",
        definition: AgentDefinition,
        agent_id: str,
        sidechain_transcript_path: Path,
        *,
        is_background: bool,
        result_summary: str = "",
        stop_reason: str | None = None,
        error: str | None = None,
    ) -> None:
        """向 HookBus 发送 SubagentStart/SubagentStop 事件。

        失败会被吞掉，因为 hook 不应该打断主执行流程。
        """
        payload = {
            "agent_name": definition.name,
            "agent_type": definition.name,
            "parent_session_id": self.session_id,
            "sidechain_path": str(sidechain_transcript_path),
            "is_background": is_background,
        }
        if result_summary:
            payload["result_summary"] = result_summary
        if stop_reason:
            payload["stop_reason"] = stop_reason
        if error:
            payload["error"] = error
        try:
            # HookInput 的 session_id 用子代理的 session_id，因为 hook 处理的是子代理事件；
            # parent_session_id 放在 payload 里，调用方仍能追溯父会话。
            await self.hook_bus.emit(
                event,  # type: ignore[arg-type]
                HookInput(
                    event,  # type: ignore[arg-type]
                    child.session_id,
                    str(self.config.cwd),
                    child.permission_context.mode,
                    transcript_path=str(sidechain_transcript_path),
                    agent_id=agent_id,
                    payload=payload,
                ),
            )
        except Exception:
            return

    def _merge_child_written_snapshots(self, child: "AgentSession") -> None:
        """把子代理写入文件后的快照合并回父会话。

        这样父会话后续编辑同一文件时，能知道最新磁盘状态来自子代理写入。
        """
        for snapshot in child.read_file_state.snapshots():
            if snapshot.source == "write":
                self.read_file_state.merge_written_snapshot(snapshot.path, snapshot)

    def _capabilities(self) -> list[str]:
        """生成要注入上下文的外部能力摘要，比如已注册技能和 MCP 状态。"""
        caps = self.skill_registry.capabilities()
        if self.config.mcp_enabled:
            if self.mcp_manager.fastmcp_available:
                caps.append("MCP external tools/resources/prompts configured")
            else:
                caps.append("MCP configured but FastMCP is not installed; MCP calls will report dependency_missing")
        return caps

    def _emit_status(self, status: str, **metadata: Any) -> None:
        """向 event_sink 发送普通状态事件；没有 event_sink 时直接忽略。"""
        if not self.event_sink:
            return
        try:
            self.event_sink(StatusEvent(session_id=self.session_id, status=status, metadata=metadata))
        except Exception:
            return

    def _emit_turn_completed(self, assistant_text: str, stop_reason: str | None, tool_result_count: int) -> None:
        """向 event_sink 发送单轮完成事件，供 JSONL 监听方消费。"""
        if not self.event_sink:
            return
        try:
            self.event_sink(
                TurnCompleted(
                    session_id=self.session_id,
                    assistant_text=assistant_text,
                    stop_reason=stop_reason,
                    tool_result_count=tool_result_count,
                )
            )
        except Exception:
            return

    def record_loaded_skill(self, name: str) -> None:
        """记录本会话已经加载过的技能，并保存快照。"""
        if name not in self.loaded_skills:
            self.loaded_skills.add(name)
            self._save_snapshot()

    def record_artifact(self, record: ArtifactRecord) -> None:
        """登记一个工具结果 artifact，保证后续 ArtifactRead 能确认它属于当前会话。"""
        self.active_artifacts[record.artifact_id] = {
            "artifact_id": record.artifact_id,
            "artifact_path": record.artifact_path,
            "original_chars": record.original_chars,
            "session_id": record.session_id,
            "tool_use_id": record.tool_use_id,
            "tool_name": record.tool_name,
            "created_at": record.created_at,
        }
        self._save_snapshot()

    def record_last_verification(self, *, command: str, exit_code: int | None) -> None:
        """记录最近一次看起来像测试/构建/检查的 Bash 命令及退出码。"""
        self.last_verification = {
            "command": command,
            "exit_code": exit_code,
            "timestamp": time.time(),
        }
        self._save_snapshot()

    def _append_transcript(self, message: MessageBase) -> None:
        """把消息追加到 transcript，并顺手保存快照。"""
        self.transcript.append(message)
        self._save_snapshot()

    def _save_snapshot(self) -> None:
        """把可恢复会话需要的状态写入 sessions/<id>.json。"""
        if not self.persist_snapshot:
            return
        save_session_snapshot(
            self.config.project_state_dir,
            SessionSnapshot(
                session_id=self.session_id,
                cwd=str(self.config.cwd),
                repo_root=str(self.config.repo_root),
                model=self.model_ref,
                permission_mode=self.permission_context.mode,
                task_list_id=self.task_list_id,
                transcript_path=str(self.transcript.path),
                message_count=len(self.messages),
                read_file_snapshots=self.read_file_state.to_snapshot(),
                loaded_skills=sorted(self.loaded_skills),
                active_artifacts=self.active_artifacts,
                last_verification=self.last_verification,
            ),
        )


def _clone_permission_context(ctx: ToolPermissionContext) -> ToolPermissionContext:
    """复制权限上下文，避免会话运行时修改污染全局配置对象。"""
    return ToolPermissionContext(
        mode=ctx.mode,
        always_allow=[PermissionRule(**rule.__dict__) for rule in ctx.always_allow],
        always_deny=[PermissionRule(**rule.__dict__) for rule in ctx.always_deny],
        always_ask=[PermissionRule(**rule.__dict__) for rule in ctx.always_ask],
        should_avoid_permission_prompts=ctx.should_avoid_permission_prompts,
    )


def _format_exception(exc: Exception) -> str:
    """把异常转成非空字符串，给命令行显示使用。"""
    return str(exc).strip() or exc.__class__.__name__


def _parse_timeout(parts: list[str]) -> float:
    """从 /doctor 参数列表里读取 --timeout，失败时回退到默认 10 秒。"""
    if "--timeout" not in parts:
        return 10.0
    idx = parts.index("--timeout")
    try:
        return float(parts[idx + 1])
    except (IndexError, ValueError):
        return 10.0


def _total_tokens(messages: list[MessageBase]) -> int:
    """从 AssistantMessage.usage 中累加 token 数，用于子代理结果统计。"""
    total = 0
    for message in messages:
        if not isinstance(message, AssistantMessage):
            continue
        for value in message.usage.values():
            if isinstance(value, int):
                total += value
    return total


def _resolve_subagent_permission_mode(parent_mode: str, definition: AgentDefinition) -> str:
    """决定子代理继承或覆盖哪种权限模式。

    父会话如果已经放宽权限，子代理也继承；否则使用子代理定义中的 permission_mode。
    """
    if parent_mode in {"bypassPermissions", "acceptEdits"}:
        return parent_mode
    return definition.permission_mode or parent_mode


def _registry_for_subagent(parent: ToolRegistry, definition: AgentDefinition, *, is_background: bool = False) -> ToolRegistry:
    """为子代理构造一个裁剪后的工具注册表。

    它会按 allowed/disallowed 工具过滤，后台任务和 plan 模式还会额外禁用危险工具。
    """
    allowed = set(definition.tools or []) if definition.tools is not None else None
    disallowed = set(definition.disallowed_tools or [])

    # 后台子代理不能再启动子代理或询问用户，否则后台任务可能无限挂起或递归扩散。
    if is_background:
        disallowed.update({"Agent", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode"})
    if definition.permission_mode == "plan":
        # plan 型子代理是只读分析角色。这里从工具层先裁掉写入、网络、任务修改等能力，
        # 权限系统里还会再用 plan mode 做第二层保护。
        disallowed.update(
            {
                "Agent",
                "Edit",
                "EnterPlanMode",
                "WritePlan",
                "ExitPlanMode",
                "AskUserQuestion",
                "TaskCreate",
                "TaskUpdate",
                "TaskClaim",
                "TaskBlock",
                "TaskBlockTask",
                "TaskStop",
                "WebFetch",
                "WebSearch",
                "Write",
            }
        )

    child = ToolRegistry()
    for tool in parent.list_tools():
        names = {tool.name, *tool.aliases}
        # allowed 存在时采用白名单；否则默认继承父 registry 中所有未禁用工具。
        if allowed is not None and not names.intersection(allowed):
            continue
        if names.intersection(disallowed):
            continue
        child.register(tool)
    return child
