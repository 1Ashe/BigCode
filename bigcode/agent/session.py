"""AgentSession 是 BigCode 的主控制器。

学习思路：一次用户提问会进入 run_turn_stream()，它负责构造上下文、请求模型、执行工具、保存 transcript/snapshot，并在需要时继续下一轮模型调用。
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from bigcode.agent.events import AgentEvent, ErrorEvent, StatusEvent, StreamEvent, ToolCompleted, TurnCompleted
from bigcode.agent.snapshot import SessionSnapshot, load_session_snapshot, save_session_snapshot
from bigcode.config.models import ResolvedModel, RuntimeConfig
from bigcode.context.builder import ContextBuildDeps, build_context_for_api
from bigcode.context.compact import ContextCompactState
from bigcode.context.messages import (
    ApiMessage,
    AssistantMessage,
    CompactRecordMessage,
    MessageBase,
    SystemPromptSnapshotMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
    text_from_blocks,
)
from bigcode.context.normalizer import tool_run_result_to_message
from bigcode.context.system_prompt import build_system_prompt
from bigcode.context.transcript import Transcript
from bigcode.hooks.builtins import register_builtin_hooks
from bigcode.hooks.models import HookInput
from bigcode.hooks import HookBus
from bigcode.mcp import McpClientManager
from bigcode.models import (
    ClaudeCompatibleModelClient,
    StreamEnd,
    TextDelta,
    ThinkingComplete,
    ToolCallComplete,
    ToolCallStart,
    create_client,
)
from bigcode.plan import PlanModeState, PlanStore
from bigcode.skills import load_skills
from bigcode.subagents.definitions import AgentDefinition
from bigcode.subagents.tasks import AgentRunResult, AgentTaskState, AgentTaskStore, render_agent_result, result_to_dict
from bigcode.tasks import TaskStore
from bigcode.tools.artifacts import ArtifactStore
from bigcode.tools import ToolExecutionContext, ToolRegistry, ToolRunner, ToolRunResult, ToolUse, build_default_registry
from bigcode.tools.mcp import register_mcp_tools_from_capabilities
from bigcode.tools.permissions import ToolPermissionContext, PermissionRule
from bigcode.tools.read_file_state import ReadFileState
from bigcode.utils.ids import new_id


TurnStopReason = Literal["end_turn", "max_steps", "unknown_tool_limit", "cancelled"]


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
        transcript_path: Path | None = None,
        persist_snapshot: bool = True,
        load_transcript: bool = True,
        abort_event: threading.Event | None = None,
        system_instruction: str | None = None,
        is_main_thread: bool = True,
    ) -> None:
        """初始化会话状态。

        这里会尝试加载已有快照，创建工具运行器、hook bus、技能注册表、MCP 管理器、transcript 和 artifact store。
        """
        self.config = config
        self.persist_snapshot = persist_snapshot
        self.is_main_thread = is_main_thread

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
        self.compact_state = ContextCompactState(
            turn_index=snapshot.compact_turn_index if snapshot else 0,
            auto_compact_failures=snapshot.compact_auto_failures if snapshot else 0,
        )

        # 下面这些字段是 resume 时最需要恢复的会话状态：
        # 文件快照保护编辑安全，技能/验证命令则影响后续上下文提醒。
        self.read_file_state = ReadFileState.from_snapshot(snapshot.read_file_snapshots) if snapshot else ReadFileState()
        self.loaded_skills: set[str] = set(snapshot.loaded_skills if snapshot else [])
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
        self.approval_callback: Any | None = None
        self.approval_cache: dict[str, bool] = {}
        self.hook_bus = HookBus()
        register_builtin_hooks(self.hook_bus)

        # transcript 是完整消息流水账，snapshot 是可快速恢复的状态摘要。
        # 两者都保存，是为了兼顾“能恢复完整历史”和“能快速列出/恢复会话”。
        self.transcript = Transcript(transcript_path or config.project_state_dir / "transcripts" / f"{self.session_id}.jsonl")
        self.artifact_store = ArtifactStore(config.project_state_dir, self.session_id)
        if session_id and load_transcript:
            self.messages = self.transcript.load()
        prompt_snapshot = next(
            (message for message in self.messages if isinstance(message, SystemPromptSnapshotMessage)),
            None,
        )
        if prompt_snapshot:
            self.system_prompt = prompt_snapshot.prompt
        elif snapshot and snapshot.system_prompt:
            self.system_prompt = snapshot.system_prompt
            prompt_snapshot = SystemPromptSnapshotMessage(self.system_prompt)
            self.messages.append(prompt_snapshot)
            self.transcript.append(prompt_snapshot)
        else:
            self.system_prompt = build_system_prompt(
                cwd=self.config.cwd,
                tool_names=[tool.name for tool in self.registry.list_tools()],
                instruction_paths=self.config.instruction_paths,
                role_instruction=system_instruction,
            ).render()
            prompt_snapshot = SystemPromptSnapshotMessage(self.system_prompt)
            self.messages.append(prompt_snapshot)
            self.transcript.append(prompt_snapshot)

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
        # 所以工具更新 read_file_state、plan_state 等状态时，AgentSession 能立刻看到。
        return ToolExecutionContext(
            cwd=self.config.cwd,
            workspace_roots=_dedupe_paths([*self.config.workspace_roots, self.config.project_state_dir]),
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
            artifact_store=self.artifact_store,
            project_state_dir=self.config.project_state_dir,
            tool_registry=self.registry,
            approval_callback=self.approval_callback,
            approval_cache=self.approval_cache,
        )

    async def start(self) -> None:
        """启动会话时的初始化动作。

        它发送 session_started 事件，触发 SessionStart hook，并保存一次快照。
        """
        await self.hook_bus.emit("SessionStart", HookInput("SessionStart", self.session_id, str(self.config.cwd), self.permission_context.mode))
        self._save_snapshot()

    async def run_turn_stream(self, prompt: str, *, max_steps: int = 20) -> AsyncIterator[AgentEvent]:
        """执行一次用户输入，并以 AgentEvent 流形式产出进度和结果。

        一次 turn 可能包含多步：模型先回复工具调用，工具执行结果再回填给模型，直到模型输出最终文本或达到 max_steps。
        """
        def complete(
            stop_reason: TurnStopReason,
            *,
            provider_stop_reason: str | None = None,
            metadata: dict[str, Any] | None = None,
        ) -> TurnCompleted:
            event_metadata = dict(metadata or {})
            if provider_stop_reason is not None:
                event_metadata["provider_stop_reason"] = provider_stop_reason
            return TurnCompleted(
                session_id=self.session_id,
                assistant_text=assistant_text,
                stop_reason=stop_reason,
                tool_result_count=len(tool_results),
                metadata=event_metadata,
            )

        yield StatusEvent(session_id=self.session_id, status="turn_started", metadata={"max_steps": max_steps})
        self.approval_cache.clear()
        self.compact_state.turn_index += 1
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
        consecutive_unknown_tools = 0
        try:
            for step in range(max_steps):
                self.compact_state.step_index = step
                yield StatusEvent(session_id=self.session_id, status="model_request_started", metadata={"step": step})
                await self._sync_mcp_tools_to_registry()

                # 每一步请求模型前都重新构建上下文，因为上一轮工具结果、hook 附件、
                # Plan Mode 状态等都可能刚刚变化。
                yield StatusEvent(session_id=self.session_id, status="compact_started", metadata={"step": step, "type": "auto"})
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
                        system_prompt=self.system_prompt,
                        compact_config=self.config.compact,
                        compact_state=self.compact_state,
                        context_window=self.model.context_window or 128000,
                        tool_schemas=self.registry.schemas_for_model(),
                        summary_callback=self._summarize_context,
                        is_main_thread=self.is_main_thread,
                        protocol=self.model.protocol,
                    ),
                )
                yield StatusEvent(
                    session_id=self.session_id,
                    status="compact_completed",
                    metadata={
                        "step": step,
                        "type": "auto",
                        "tokens_before": built.compact_result.tokens_before,
                        "tokens_after": built.compact_result.tokens_after,
                        "blocked": built.compact_result.blocked,
                    },
                )
                self._append_compact_records(built.compact_result.records_to_append)
                if built.compact_result.blocked:
                    message = (
                        "Context is too large after compaction "
                        f"({built.compact_result.utilization_after:.1%} of the model context window)."
                    )
                    yield StatusEvent(
                        session_id=self.session_id,
                        status="context_compaction_blocked",
                        metadata={"step": step, "tokens_before": built.compact_result.tokens_before, "tokens_after": built.compact_result.tokens_after},
                    )
                    raise RuntimeError(message)

                assistant: AssistantMessage | None = None
                buffered_events: list[AgentEvent] = []
                async for item in self._request_model_stream(
                    built.system_prompt,
                    built.api_messages,
                    self.registry.schemas_for_model(),
                ):
                    if isinstance(item, AssistantMessage):
                        assistant = item
                    else:
                        buffered_events.append(item)
                if assistant is None:
                    raise RuntimeError("Model stream ended without an assistant message.")

                yield StatusEvent(session_id=self.session_id, status="model_response_completed", metadata={"step": step})
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
                    for event in buffered_events:
                        yield event
                    yield StatusEvent(
                        session_id=self.session_id,
                        status="turn_completed",
                        metadata={"stop_reason": "end_turn", "provider_stop_reason": assistant.stop_reason},
                    )
                    yield complete("end_turn", provider_stop_reason=assistant.stop_reason)
                    return
                turn_tool_names.extend(t.name for t in tool_uses)

                # 模型请求工具时，不直接把结果返回给用户；先执行工具，再把 tool_result
                # 作为新的用户侧 meta 消息塞回 messages，让模型基于结果继续推理。
                step_results: list[ToolRunResult[Any]] = []
                async for item in self.runner.run_tool_uses(tool_uses, self.make_tool_context()):
                    if isinstance(item, ToolRunResult):
                        step_results.append(item)
                        if item.metadata.get("unknown_tool") is True:
                            consecutive_unknown_tools += 1
                        else:
                            consecutive_unknown_tools = 0
                    else:
                        yield item
                tool_results.extend(step_results)
                for result in step_results:
                    result_msg = tool_run_result_to_message(result)
                    self.messages.append(result_msg)
                    self._append_transcript(result_msg)
                if consecutive_unknown_tools >= 3:
                    message = "Stopping turn after 3 consecutive unknown tool calls."
                    yield ErrorEvent(
                        session_id=self.session_id,
                        message=message,
                        metadata={"limit": 3, "consecutive_unknown_tools": consecutive_unknown_tools},
                    )
                    yield complete(
                        "unknown_tool_limit",
                        metadata={"limit": 3, "consecutive_unknown_tools": consecutive_unknown_tools},
                    )
                    return
            yield StatusEvent(session_id=self.session_id, status="turn_max_steps", metadata={"max_steps": max_steps})
            yield complete("max_steps", metadata={"max_steps": max_steps})
        except asyncio.CancelledError:
            self.abort_event.set()
            yield ErrorEvent(session_id=self.session_id, message="Operation cancelled")
            yield complete("cancelled")
            return

    async def _request_model_stream(
        self,
        system_prompt: str,
        api_messages: list[Any],
        tool_schemas: list[dict[str, Any]],
    ) -> AsyncIterator[AgentEvent | AssistantMessage]:
        """请求模型流并收集成一条 AssistantMessage。"""
        text_parts: list[str] = []
        thinking_blocks: list[ThinkingBlock] = []
        tool_blocks: list[ToolUseBlock] = []
        stop_reason: str | None = None
        usage: dict[str, Any] = {}

        client = _create_model_client(self.model)
        async for event in client.stream(system_prompt, api_messages, tool_schemas):
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
                yield StreamEvent(session_id=self.session_id, text=event.text)
            elif isinstance(event, ThinkingComplete):
                thinking_blocks.append(ThinkingBlock(thinking=event.thinking, signature=event.signature))
            elif isinstance(event, ToolCallStart):
                yield StatusEvent(
                    session_id=self.session_id,
                    status="model_tool_call_started",
                    metadata={"tool_use_id": event.id, "tool_name": event.name},
                )
            elif isinstance(event, ToolCallComplete):
                tool_blocks.append(ToolUseBlock(id=event.id, name=event.name, input=event.input))
            elif isinstance(event, StreamEnd):
                stop_reason = event.stop_reason
                usage = {"input_tokens": event.input_tokens, "output_tokens": event.output_tokens}

        content = []
        content.extend(thinking_blocks)
        if text_parts:
            content.append(TextBlock(text="".join(text_parts)))
        content.extend(tool_blocks)
        yield AssistantMessage(content, model=self.model.ref, stop_reason=stop_reason, usage=usage)

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
            transcript_path=sidechain_transcript_path,
            persist_snapshot=False,
            load_transcript=False,
            abort_event=child_abort_event,
            system_instruction=definition.system_prompt,
            is_main_thread=False,
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
            assistant_text = ""
            tool_use_count = 0
            async for event in child.run_turn_stream(prompt, max_steps=definition.max_turns):
                if isinstance(event, ToolCompleted):
                    tool_use_count += 1
                elif isinstance(event, TurnCompleted):
                    assistant_text = event.assistant_text
                    stop_reason = event.stop_reason
            result_summary = assistant_text
            run_result = AgentRunResult(
                agent_id=agent_id,
                agent_type=definition.name,
                content=assistant_text,
                total_tool_use_count=tool_use_count,
                total_duration_ms=int((time.perf_counter() - started) * 1000),
                total_tokens=_total_tokens(child.messages),
                stop_reason=stop_reason,
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
                for tool in self.registry.deferred_tools():
                    if getattr(tool, "is_mcp", False):
                        caps.append(f"Deferred MCP tool available via Tool_Search: {tool.name}")
            else:
                caps.append("MCP configured but FastMCP is not installed; MCP calls will report dependency_missing")
        return caps

    async def _sync_mcp_tools_to_registry(self) -> list[str]:
        """发现 MCP tools，并把它们注册成可被 Tool_Search 加载的动态工具。"""
        if not self.config.mcp_enabled or not self.mcp_manager.fastmcp_available:
            return []
        try:
            capabilities = await self.mcp_manager.discover()
        except Exception:
            return []
        return register_mcp_tools_from_capabilities(self.registry, capabilities)

    def model_protocol_label(self) -> str:
        """返回当前模型协议；配置错误时返回占位文本。"""
        if not self.model_ref:
            return "(none)"
        try:
            return self.model.protocol
        except Exception:
            return "(invalid)"

    def record_loaded_skill(self, name: str) -> None:
        """记录本会话已经加载过的技能，并保存快照。"""
        if name not in self.loaded_skills:
            self.loaded_skills.add(name)
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

    def _append_compact_records(self, records: list[CompactRecordMessage]) -> None:
        """把本轮新生成的压缩事件追加到 UI Context 和 transcript。"""
        for record in records:
            self.messages.append(record)
            self.transcript.append(record)
        if records:
            self._save_snapshot()

    async def _summarize_context(self, compact_type: str, content: str) -> str:
        """用独立模型请求生成 Collapse/Auto Compact 摘要。"""
        if compact_type == "collapse":
            instruction = (
                "Create a concise context-collapse summary. Preserve user intent, active goals, "
                "tool calls and results that still matter, file paths and edits, exact relevant "
                "errors, decisions, current state, and pending work."
            )
        else:
            instruction = (
                "Create a structured conversation summary with sections for Primary Request, "
                "Key Decisions, Files Read or Modified, Errors Encountered, Current State, and "
                "Pending Tasks. Preserve exact paths and important error text."
            )
        messages: list[Any]
        if self.model.protocol == "openai":
            messages = [{"role": "user", "content": content}]
        else:
            messages = [ApiMessage(role="user", content=[{"type": "text", "text": content}])]
        assistant: AssistantMessage | None = None
        async for item in self._request_model_stream(instruction, messages, []):
            if isinstance(item, AssistantMessage):
                assistant = item
        if assistant is None:
            return ""
        return text_from_blocks(assistant.content)

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
                last_verification=self.last_verification,
                system_prompt=self.system_prompt,
                compact_auto_failures=self.compact_state.auto_compact_failures,
                compact_turn_index=self.compact_state.turn_index,
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


def _create_model_client(model: ResolvedModel) -> Any:
    """创建模型客户端；测试替换旧 complete 客户端时自动适配成流式接口。"""
    if getattr(ClaudeCompatibleModelClient, "__module__", "") != "bigcode.models.claude_compatible":
        return _CompleteClientAdapter(ClaudeCompatibleModelClient(model))
    return create_client(model)


class _CompleteClientAdapter:
    """把旧 complete() 客户端适配成 stream()，用于保留旧测试和扩展兼容性。"""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def stream(
        self,
        system_prompt: str,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        response = await self.client.complete(system_prompt, messages, tools or [])
        usage = response.message.usage or {}
        for block in response.message.content:
            if isinstance(block, TextBlock):
                yield TextDelta(text=block.text)
            elif isinstance(block, ThinkingBlock):
                yield ThinkingComplete(thinking=block.thinking, signature=block.signature)
            elif isinstance(block, ToolUseBlock):
                yield ToolCallStart(id=block.id, name=block.name)
                yield ToolCallComplete(id=block.id, name=block.name, input=block.input)
        yield StreamEnd(
            stop_reason=response.message.stop_reason,
            input_tokens=usage.get("input_tokens", 0) if isinstance(usage.get("input_tokens"), int) else 0,
            output_tokens=usage.get("output_tokens", 0) if isinstance(usage.get("output_tokens"), int) else 0,
        )


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        resolved = path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


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
    child.inherit_discoveries_from(parent)
    return child
