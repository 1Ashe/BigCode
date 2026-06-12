from __future__ import annotations

from bigcode.tasks.store import TaskStore
from bigcode.tools.base import ToolExecutionContext


def task_store(ctx: ToolExecutionContext) -> TaskStore:
    if not ctx.task_store:
        raise RuntimeError("Task store is not configured.")
    return ctx.task_store


def task_list_id(ctx: ToolExecutionContext) -> str:
    return getattr(ctx.agent_session, "task_list_id", None) or ctx.session_id
