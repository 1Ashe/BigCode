"""把任务列表保存为本地 JSON 文件。

学习思路：每个任务一个 json 文件，锁用来避免同一个任务列表被并发修改时出现竞争。
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

from bigcode.utils.jsonio import read_json_file, write_json_file

from .models import ClaimResult, TaskItem


_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")


class TaskStore:
    """本地状态存储封装。

    它把文件路径、读写和必要的并发保护集中起来，调用方不用关心磁盘细节。
    """
    def __init__(self, root: Path) -> None:
        """保存任务根目录，并准备每个任务列表独立使用的锁。"""
        self.root = root
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.RLock()

    def sanitize_list_id(self, task_list_id: str) -> str:
        """把任务列表 id 转成安全目录名。"""
        return _SAFE_RE.sub("-", task_list_id).strip("-") or "default"

    def create(self, task_list_id: str, data) -> str:
        """在指定任务列表中创建新任务，并更新 highwatermark。"""
        task_list_id = self.sanitize_list_id(task_list_id)
        with self._lock_for(task_list_id):
            path = self._dir(task_list_id)
            path.mkdir(parents=True, exist_ok=True)
            next_id = self._next_id(path)
            item = TaskItem(
                id=str(next_id),
                subject=data.subject,
                description=data.description,
                active_form=data.active_form,
                metadata=data.metadata or {},
            )
            write_json_file(path / f"{next_id}.json", item)
            write_json_file(path / ".highwatermark", {"value": next_id})
            return str(next_id)

    def get(self, task_list_id: str, task_id: str) -> TaskItem | None:
        """按任务列表和任务 id 读取单个任务。"""
        data, _ = read_json_file(self._dir(self.sanitize_list_id(task_list_id)) / f"{task_id}.json")
        return _task_from_dict(data) if data else None

    def list(self, task_list_id: str) -> list[TaskItem]:
        """列出指定任务列表下的所有任务，按数字 id 排序。"""
        path = self._dir(self.sanitize_list_id(task_list_id))
        items: list[TaskItem] = []
        for file in sorted(path.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else 10**9):
            data, _ = read_json_file(file)
            if data:
                items.append(_task_from_dict(data))
        return items

    def update(self, task_list_id: str, task_id: str, updates) -> TaskItem | None:
        """局部更新任务字段，并写回磁盘。"""
        task_list_id = self.sanitize_list_id(task_list_id)
        with self._lock_for(task_list_id):
            item = self.get(task_list_id, task_id)
            if not item:
                return None
            for field in ["status", "subject", "description", "active_form", "owner", "blocks", "blocked_by"]:
                value = getattr(updates, field, None)
                if value is not None:
                    setattr(item, field, value)
            if getattr(updates, "clear_owner", False):
                item.owner = None
            write_json_file(self._dir(task_list_id) / f"{task_id}.json", item)
            return item

    def claim(self, task_list_id: str, task_id: str, owner: str, check_busy: bool = False) -> ClaimResult:
        """原子领取一个 pending 且未被阻塞的任务。"""
        task_list_id = self.sanitize_list_id(task_list_id)
        with self._lock_for(task_list_id):
            item = self.get(task_list_id, task_id)
            if not item:
                return ClaimResult(False, f"Task {task_id} does not exist.")
            if item.status != "pending":
                return ClaimResult(False, f"Task {task_id} is not pending.", item)
            if item.owner not in {None, owner}:
                return ClaimResult(False, f"Task {task_id} is already owned by {item.owner}.", item)
            blocker = self._first_unfinished_blocker(task_list_id, item)
            if blocker:
                return ClaimResult(False, f"Task {task_id} is blocked by unfinished task {blocker}.", item)
            if check_busy:
                for other in self.list(task_list_id):
                    if other.id != task_id and other.owner == owner and other.status != "completed":
                        return ClaimResult(False, f"Owner {owner} already has unfinished task {other.id}.", item)
            item.owner = owner
            item.status = "in_progress"
            write_json_file(self._dir(task_list_id) / f"{task_id}.json", item)
            return ClaimResult(True, task=item)

    def block_task(self, task_list_id: str, from_task_id: str, to_task_id: str) -> tuple[TaskItem, TaskItem]:
        """记录 from_task 阻塞 to_task，并维护两个方向的依赖字段。"""
        task_list_id = self.sanitize_list_id(task_list_id)
        with self._lock_for(task_list_id):
            source = self.get(task_list_id, from_task_id)
            target = self.get(task_list_id, to_task_id)
            if not source:
                raise RuntimeError(f"Task {from_task_id} does not exist.")
            if not target:
                raise RuntimeError(f"Task {to_task_id} does not exist.")
            if to_task_id not in source.blocks:
                source.blocks.append(to_task_id)
            if from_task_id not in target.blocked_by:
                target.blocked_by.append(from_task_id)
            write_json_file(self._dir(task_list_id) / f"{from_task_id}.json", source)
            write_json_file(self._dir(task_list_id) / f"{to_task_id}.json", target)
            return source, target

    def delete(self, task_list_id: str, task_id: str) -> bool:
        """删除一个任务 JSON 文件，返回是否真的删除。"""
        path = self._dir(self.sanitize_list_id(task_list_id)) / f"{task_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    def reset(self, task_list_id: str) -> None:
        """删除指定任务列表中的所有任务 JSON 文件。"""
        path = self._dir(self.sanitize_list_id(task_list_id))
        if not path.exists():
            return
        for file in path.glob("*.json"):
            file.unlink()

    def _dir(self, task_list_id: str) -> Path:
        """计算某个任务列表对应的目录路径。"""
        return self.root / self.sanitize_list_id(task_list_id)

    def _lock_for(self, task_list_id: str) -> threading.RLock:
        """取得某个任务列表专用的锁，避免并发写同一组任务。"""
        with self._guard:
            lock = self._locks.get(task_list_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[task_list_id] = lock
            return lock

    def _next_id(self, path: Path) -> int:
        """根据 highwatermark 和现有文件计算下一个任务数字 id。"""
        hw, _ = read_json_file(path / ".highwatermark")
        high = int(hw.get("value", 0)) if hw else 0
        existing = [int(p.stem) for p in path.glob("*.json") if p.stem.isdigit()]
        return max([high, *existing], default=0) + 1

    def _first_unfinished_blocker(self, task_list_id: str, item: TaskItem) -> str | None:
        """返回第一个还未完成的阻塞任务 id，没有则返回 None。"""
        for blocker_id in item.blocked_by:
            blocker = self.get(task_list_id, blocker_id)
            if blocker and blocker.status != "completed":
                return blocker_id
        return None


def _task_from_dict(data: dict) -> TaskItem:
    """把任务 JSON dict 转回 TaskItem。"""
    return TaskItem(
        id=str(data["id"]),
        subject=data.get("subject", ""),
        description=data.get("description", ""),
        status=data.get("status", "pending"),
        active_form=data.get("active_form"),
        owner=data.get("owner"),
        blocks=list(data.get("blocks") or []),
        blocked_by=list(data.get("blocked_by") or []),
        metadata=dict(data.get("metadata") or {}),
    )
