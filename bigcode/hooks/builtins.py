"""内置 Hook：计划模式提醒、任务提醒和能力索引。

学习思路：这些 hook 不直接改主流程，而是通过 Attachment 或 continue_turn 影响下一次模型上下文。
"""
from __future__ import annotations

from bigcode.context.attachments import Attachment

from .bus import HookBus, HookHandler
from .models import HookInput, HookOutput


class PlanModeContextHook(HookHandler):
    """计划模式上下文 Hook。

    它在构建上下文时注入“只能读、不能改”的提醒，并在退出计划模式后注入已批准计划。
    """
    name = "PlanModeContextHook"
    events = ("ContextBuild", "PlanModeExit")
    priority = 10

    async def run(self, input: HookInput) -> HookOutput:
        """根据 PlanModeState 决定是否注入计划模式提醒或已批准计划。"""
        state = input.payload.get("plan_mode_state")
        if not state:
            return HookOutput()
        attachments = []
        if getattr(state, "active", False):
            # active=True 表示当前仍在计划模式，每次构建上下文都要提醒模型只读。
            plan_file = getattr(state, "plan_file", None)
            attachments.append(
                Attachment(
                    type="plan_mode",
                    text=(
                        "You are in Plan Mode. Read and inspect as needed, but do not edit workspace files "
                        f"or execute mutating commands. Write the final implementation plan to {plan_file}. "
                        "Continue the iterative workflow: explore the codebase, ask user questions for unresolved "
                        "decisions, and update the plan incrementally. End planning by calling AskUserQuestion for "
                        "clarifications or ExitPlanMode for approval."
                    ),
                    source=self.name,
                )
            )
        if getattr(state, "needs_exit_attachment", False):
            # ExitPlanMode 批准后，下一轮上下文需要告诉模型“现在可以实现了”，
            # 但这个提醒只需要注入一次，所以后面会把标志清掉。
            approved = getattr(state, "approved_plan", "") or ""
            attachments.append(
                Attachment(type="plan_mode_exit", text=f"Plan Mode has ended. You may now implement the approved plan.\n\nApproved plan:\n{approved}", source=self.name)
            )
            state.needs_exit_attachment = False
        return HookOutput(attachments=attachments)


class PlanModeStopHook(HookHandler):
    """计划模式停止保护 Hook。

    如果模型在 Plan Mode 中没有用 AskUserQuestion 或 ExitPlanMode 收尾，它会要求继续当前 turn。
    """
    name = "PlanModeStopHook"
    events = ("Stop",)
    priority = 10

    async def run(self, input: HookInput) -> HookOutput:
        """在 Stop 事件中检查 Plan Mode 是否正确收尾。"""
        state = input.payload.get("plan_mode_state")
        if not state or not getattr(state, "active", False):
            return HookOutput()
        tool_names = set(input.payload.get("turn_tool_names") or [])
        if tool_names.intersection({"AskUserQuestion", "ExitPlanMode"}):
            return HookOutput()
        return HookOutput(
            decision="block",
            continue_turn=True,
            reason="Plan Mode turns must continue with AskUserQuestion or ExitPlanMode.",
            additional_context=(
                "Plan Mode is still active. Continue read-only exploration, update the plan, "
                "or call AskUserQuestion / ExitPlanMode. Do not ask for plan approval in plain text."
            ),
        )


class TaskReminderHook(HookHandler):
    """任务提醒 Hook。

    构建上下文时把当前未完成任务列表注入给模型，避免模型忘记任务状态。
    """
    name = "TaskReminderHook"
    events = ("ContextBuild",)
    priority = 30

    async def run(self, input: HookInput) -> HookOutput:
        """读取当前任务列表，把未完成任务整理成 Attachment。"""
        store = input.payload.get("task_store")
        task_list_id = input.payload.get("task_list_id")
        if not store or not task_list_id:
            return HookOutput()
        try:
            tasks = [t for t in store.list(task_list_id) if t.status != "completed" and not t.metadata.get("_internal")]
        except Exception:
            return HookOutput()
        if not tasks:
            return HookOutput()
        lines = ["Current unfinished tasks:"]
        lines.extend(f"- [{t.status}] {t.id}: {t.subject}" for t in tasks[:20])
        return HookOutput(attachments=[Attachment(type="todo_reminder", text="\n".join(lines), source=self.name)])


class CapabilityIndexHook(HookHandler):
    """能力索引 Hook。

    每个会话首次构建上下文时，向模型提示可按需加载的技能或外部能力。
    """
    name = "CapabilityIndexHook"
    events = ("ContextBuild", "CapabilityChanged", "SessionStart")
    priority = 40

    def __init__(self) -> None:
        """记录哪些 session 已经注入过能力索引，避免每轮都重复提示。"""
        self._injected_sessions: set[str] = set()

    async def run(self, input: HookInput) -> HookOutput:
        """首次 ContextBuild 时，把可用技能/MCP 能力摘要注入上下文。"""
        if input.hook_event_name != "ContextBuild" or input.session_id in self._injected_sessions:
            return HookOutput()
        caps = input.payload.get("capabilities") or []
        if not caps:
            return HookOutput()
        self._injected_sessions.add(input.session_id)
        lines = ["Untrusted external capabilities available; load only when relevant:"]
        lines.extend(f"- {cap}" for cap in sorted(caps, key=str.casefold)[:80])
        return HookOutput(attachments=[Attachment(type="capability_index", text="\n".join(lines), source=self.name)])


def register_builtin_hooks(bus: HookBus) -> None:
    """把所有内置 hook 注册到 HookBus。"""
    bus.register(PlanModeContextHook())
    bus.register(PlanModeStopHook())
    bus.register(TaskReminderHook())
    bus.register(CapabilityIndexHook())
