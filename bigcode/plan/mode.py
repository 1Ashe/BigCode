"""Plan Mode 的内存状态。

学习思路：进入计划模式后权限会临时变成 plan，退出后再恢复到进入前的权限模式。
"""
from __future__ import annotations

from dataclasses import dataclass

from bigcode.tools.permissions import PermissionMode


@dataclass
class PlanModeState:
    """运行时状态对象。

    字段主要记录当前流程走到哪里，通常会被会话或工具持续更新。
    """
    active: bool = False
    pre_plan_permission_mode: PermissionMode | None = None
    plan_file: str | None = None
    plan_slug: str | None = None
    approved_plan: str | None = None
    has_exited_plan_mode: bool = False
    needs_exit_attachment: bool = False

