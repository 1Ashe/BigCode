"""计划文件的读写封装。

学习思路：PlanStore 根据 session_id 生成稳定文件名，把计划内容存到 .bigcode/plans。
"""
from __future__ import annotations

from pathlib import Path

from bigcode.utils.ids import safe_slug


class PlanStore:
    """本地状态存储封装。

    它把文件路径、读写和必要的并发保护集中起来，调用方不用关心磁盘细节。
    """
    def __init__(self, root: Path) -> None:
        """保存计划文件根目录。"""
        self.root = root

    def get_slug(self, session_id: str) -> str:
        """把 session_id 转成适合作为文件名的 slug。"""
        return safe_slug(session_id)

    def get_path(self, session_id: str, agent_id: str | None = None) -> Path:
        """根据 session_id 和可选 agent_id 计算计划文件路径。"""
        slug = self.get_slug(session_id)
        if agent_id:
            slug = f"{slug}-agent-{safe_slug(agent_id)}"
        return self.root / f"{slug}.md"

    def read(self, session_id: str, agent_id: str | None = None) -> str | None:
        """读取计划文件内容；文件不存在时返回 None。"""
        path = self.get_path(session_id, agent_id)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def write(self, session_id: str, content: str, agent_id: str | None = None) -> Path:
        """写入计划文件，并确保父目录存在。"""
        path = self.get_path(session_id, agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def clear_slug(self, session_id: str) -> None:
        """删除当前 session 对应的计划文件。"""
        path = self.get_path(session_id)
        if path.exists():
            path.unlink()

