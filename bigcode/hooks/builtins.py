"""内置 Hook：计划模式提醒、任务提醒和能力索引。

学习思路：这些 hook 不直接改主流程，而是通过 Attachment 或 continue_turn 影响下一次模型上下文。
"""
from __future__ import annotations

from bigcode.context.attachments import Attachment

from .bus import HookBus, HookHandler
from .models import HookInput, HookOutput


_PLAN_MODE_FULL_REMINDER = """\
Plan mode is active. The user indicated that they do not want broad implementation yet. Prefer read-only exploration and planning. Do not proactively edit non-plan workspace files.

## Plan File Info:
{plan_file_info}
Build your plan incrementally by writing to or editing this file with the normal Write/Edit tools. Writes to this exact plan file are automatically allowed in Plan Mode. Other writes, commands, network calls, and agent actions use normal permission prompts; if you truly need a small supporting change during planning, request the tool call and the user can approve it.

## Plan Workflow

### Phase 1: Initial Understanding
Goal: Understand the user's request and the relevant code.
- Search and read the codebase before proposing changes.
- Reuse existing functions, utilities, and patterns where possible.
- Ask the user only for decisions that cannot be resolved from the repository.

### Phase 2: Design
Goal: Design the implementation approach.
- Decide the exact behavior, touched subsystems, data flow, edge cases, and verification.
- Keep scope tight; do not add unrelated refactors.

### Phase 3: Review
Goal: Ensure the plan aligns with the user's intent.
- Re-read critical files if needed.
- Check that the plan is decision-complete.

### Phase 4: Final Plan
Goal: Write the final plan to the plan file.
- Include the recommended approach only.
- Include important interface/type/API changes.
- Include verification steps and assumptions."""

_PLAN_MODE_SPARSE_REMINDER = (
    "Plan mode still active. Plan file: {plan_path}. Prefer read-only planning; write the plan with Write/Edit. "
    "Writes to the plan file are auto-allowed, other non-read actions use normal permission prompts."
)

_PLAN_REMINDER_INTERVAL = 5


def build_plan_mode_reminder(plan_path: str, plan_exists: bool, iteration: int) -> str:
    """Return the full or sparse Plan Mode reminder using mewcode's cadence."""

    if plan_exists:
        plan_file_info = (
            f"Plan file: {plan_path}\n"
            f"A plan file already exists at {plan_path}. Read it if needed and update it with Edit or Write."
        )
    else:
        plan_file_info = (
            f"Plan file: {plan_path}\n"
            f"No plan file exists yet. Create it at {plan_path} with the Write tool."
        )

    if iteration == 1:
        return _PLAN_MODE_FULL_REMINDER.format(plan_file_info=plan_file_info)

    attachment_index = (iteration - 1) // _PLAN_REMINDER_INTERVAL
    if attachment_index % _PLAN_REMINDER_INTERVAL == 0:
        return _PLAN_MODE_FULL_REMINDER.format(plan_file_info=plan_file_info)

    return _PLAN_MODE_SPARSE_REMINDER.format(plan_path=plan_path)


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
            plan_file = getattr(state, "plan_file", None)
            if plan_file:
                iteration = int(input.payload.get("step_index") or input.payload.get("turn_index") or 1)
                plan_exists = bool(input.payload.get("plan_file_exists"))
                reminder = build_plan_mode_reminder(str(plan_file), plan_exists, iteration)
            else:
                reminder = "Plan mode is active. Prefer read-only planning; non-read actions use normal permission prompts."
            attachments.append(
                Attachment(
                    type="plan_mode",
                    text=reminder,
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
    bus.register(TaskReminderHook())
    bus.register(CapabilityIndexHook())
