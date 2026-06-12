"""任务系统的数据模型。

学习思路：TaskItem 表示一项待办，blocks/blocked_by 用来表达任务之间的依赖关系。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


TaskStatus = Literal["pending", "in_progress", "completed"]


@dataclass
class TaskItem:
    """结构化结果数据。

    这种 dataclass 主要用于在模块之间传递信息，比直接使用 dict 更容易看出字段含义。
    """
    id: str
    subject: str
    description: str
    status: TaskStatus = "pending"
    active_form: str | None = None
    owner: str | None = None
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClaimResult:
    """结构化结果数据。

    这种 dataclass 主要用于在模块之间传递信息，比直接使用 dict 更容易看出字段含义。
    """
    claimed: bool
    reason: str = ""
    task: TaskItem | None = None
