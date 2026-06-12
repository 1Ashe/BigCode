from __future__ import annotations

from bigcode.subagents.tasks import AgentTaskStore
from bigcode.tools.base import ToolExecutionContext


def agent_task_store(ctx: ToolExecutionContext) -> AgentTaskStore:
    if ctx.agent_session is not None and hasattr(ctx.agent_session, "agent_task_store"):
        return ctx.agent_session.agent_task_store
    if ctx.project_state_dir is not None:
        return AgentTaskStore(ctx.project_state_dir)
    raise RuntimeError("Background subAgent task store is not configured.")
