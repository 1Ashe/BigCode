"""skills 子包的对外导出。

学习思路：load_skills() 扫描磁盘技能，SkillRegistry 在会话运行时提供查询和能力摘要。
"""

from .loader import SkillRegistry, load_skills

__all__ = ["SkillRegistry", "load_skills"]
