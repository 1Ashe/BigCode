"""技能系统的数据模型。

学习思路：SkillDefinition 描述一个可加载技能，SkillLoadReport 记录扫描过程中的成功、禁用或失败。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class SkillDefinition:
    """一个已注册技能的定义。

    root 指技能目录，skill_md 指说明文件，resources 列出可额外读取的资源。
    """
    name: str
    root: Path
    skill_md: Path
    description: str = ""
    resources: list[str] = field(default_factory=list)
    source: str = "skill"
    plugin_name: str | None = None


SkillLoadStatus = Literal["enabled", "disabled", "failed"]


@dataclass(frozen=True)
class SkillLoadReport:
    """结构化结果数据。

    这种 dataclass 主要用于在模块之间传递信息，比直接使用 dict 更容易看出字段含义。
    """
    name: str
    status: SkillLoadStatus
    source: str
    path: str
    reason: str = ""
    plugin_name: str | None = None
