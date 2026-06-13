"""记录文件读取快照，保护后续编辑。

学习思路：读文件会记录大小、mtime、hash；写或编辑前会校验文件没有被外部修改。
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class FileSnapshot:
    """文件某一时刻的快照。

    包含 size、mtime、sha256 和读取范围，用于判断后续编辑前文件是否变化。
    """
    path: Path
    content: str | None
    exists: bool
    mtime_ns: int | None
    size: int | None
    sha256: str | None
    content_digest: str | None
    offset: int | None
    limit: int | None
    view_kind: Literal["full", "range", "summary", "partial"]
    source: Literal["read", "write", "auto_inject", "summary"]
    is_partial_view: bool
    read_at: float


@dataclass(frozen=True)
class DuplicateReadHit:
    """重复读取命中结果。

    如果同一范围已经读过且磁盘未变化，ReadTool 可以返回轻量提示而不是重复内容。
    """
    path: Path
    offset: int | None
    limit: int | None
    message: str = "File content for this range is already in context and unchanged on disk."


class ReadFileState:
    """会话内的文件读取快照管理器。

    Read/Edit/Write 都依赖它判断文件是否已经读过、是否被外部修改过，以及是否可以安全覆盖。
    """
    def __init__(self, *, max_entries: int = 100) -> None:
        """初始化 LRU 式文件快照表和每个文件使用的重入锁。"""
        self._snapshots: OrderedDict[Path, FileSnapshot] = OrderedDict()
        self._locks: dict[Path, threading.RLock] = {}
        self._guard = threading.RLock()
        self.max_entries = max_entries

    def clone(self) -> "ReadFileState":
        """复制当前快照表，常用于子代理从父会话继承读文件状态。"""
        other = ReadFileState(max_entries=self.max_entries)
        with self._guard:
            other._snapshots = OrderedDict(self._snapshots)
        return other

    def to_snapshot(self) -> list[dict[str, Any]]:
        """把内存快照转成可写入 session snapshot 的 dict 列表。"""
        with self._guard:
            return [_file_snapshot_to_dict(snapshot) for snapshot in self._snapshots.values()]

    def snapshots(self) -> list[FileSnapshot]:
        """返回当前保存的 FileSnapshot 列表。"""
        with self._guard:
            return list(self._snapshots.values())

    def clear(self) -> None:
        """清空所有读取快照。compact 后必须重新读取文件才能编辑。"""
        with self._guard:
            self._snapshots.clear()

    @classmethod
    def from_snapshot(cls, data: list[dict[str, Any]] | None, *, max_entries: int = 100) -> "ReadFileState":
        """从 session snapshot 中恢复 ReadFileState。"""
        state = cls(max_entries=max_entries)
        for item in data or []:
            snapshot = _file_snapshot_from_dict(item)
            if snapshot is not None:
                state.record_read(snapshot.path, snapshot)
        return state

    def merge_written_snapshot(self, path: Path, snapshot: FileSnapshot | None) -> None:
        """把另一个会话写入后的快照合并进当前状态。"""
        if snapshot:
            self.record_read(path, snapshot)

    def record_read(self, path: Path, snapshot: FileSnapshot) -> None:
        """记录某个文件的一次读取或写入快照，并维护最多 max_entries 条。"""
        path = path.resolve(strict=False)
        with self._guard:
            self._snapshots[path] = snapshot
            self._snapshots.move_to_end(path)
            while len(self._snapshots) > self.max_entries:
                self._snapshots.popitem(last=False)

    def get_snapshot(self, path: Path) -> FileSnapshot | None:
        """读取某个文件最近一次快照，并把它移动到 LRU 末尾。"""
        path = path.resolve(strict=False)
        with self._guard:
            snap = self._snapshots.get(path)
            if snap:
                self._snapshots.move_to_end(path)
            return snap

    def check_duplicate_read(self, path: Path, offset: int | None, limit: int | None) -> DuplicateReadHit | None:
        """判断同一文件范围是否已经读过且磁盘未变化。"""
        snap = self.get_snapshot(path)
        if not snap or snap.source != "read":
            return None
        if snap.is_partial_view and snap.offset is None and snap.limit is None:
            return None
        if snap.offset != offset or snap.limit != limit:
            return None
        if not self._matches_disk(snap):
            return None
        return DuplicateReadHit(path=path, offset=offset, limit=limit)

    def validate_unchanged(self, path: Path) -> None:
        """编辑/覆盖前校验文件已读过且磁盘内容未变。"""
        snap = self.get_snapshot(path)
        if not snap:
            raise RuntimeError("File must be read before editing.")
        if snap.is_partial_view or snap.offset is not None or snap.limit is not None:
            raise RuntimeError("File must be fully read before editing.")
        if not self._matches_disk(snap):
            raise RuntimeError("File changed on disk since it was read.")

    def refresh_after_write(self, path: Path, content: str) -> FileSnapshot:
        """写入成功后重新生成快照，并标记来源为 write。"""
        snapshot = make_snapshot(path, content=content, content_digest=digest_text(content), offset=None, limit=None, source="write", is_partial_view=False)
        self.record_read(path, snapshot)
        return snapshot

    def mark_partial_view(self, path: Path) -> None:
        """把某个快照降级为部分视图，常用于 Read 结果被 artifact 截断后。"""
        path = path.resolve(strict=False)
        with self._guard:
            snap = self._snapshots.get(path)
            if snap is None:
                return
            self._snapshots[path] = replace(snap, view_kind="partial", is_partial_view=True)
            self._snapshots.move_to_end(path)

    def lock_for(self, path: Path) -> threading.RLock:
        """返回某个文件专用的重入锁，用于串行化读写状态检查。"""
        path = path.resolve(strict=False)
        with self._guard:
            lock = self._locks.get(path)
            if lock is None:
                lock = threading.RLock()
                self._locks[path] = lock
            return lock

    def _matches_disk(self, snap: FileSnapshot) -> bool:
        """判断快照描述的文件状态是否仍和磁盘一致。"""
        if not snap.path.exists():
            return not snap.exists
        stat = snap.path.stat()
        if stat.st_size != snap.size:
            return False
        if stat.st_mtime_ns != snap.mtime_ns:
            # Windows 上 mtime 可能虚报变化；用读取时保存的内容做最终兜底。
            if snap.content is None:
                return sha256_file(snap.path) == snap.sha256
            try:
                return snap.path.read_text(encoding="utf-8", errors="replace") == snap.content
            except OSError:
                return False
        return True


def make_snapshot(
    path: Path,
    *,
    content: str | None,
    content_digest: str | None,
    offset: int | None,
    limit: int | None,
    source: Literal["read", "write", "auto_inject", "summary"],
    is_partial_view: bool,
) -> FileSnapshot:
    """读取文件 stat/hash 信息并构造 FileSnapshot。"""
    path = path.resolve(strict=False)
    exists = path.exists()
    stat = path.stat() if exists else None

    # sha256 只在文件存在且是普通文件时计算。目录、缺失路径没有内容 hash。
    return FileSnapshot(
        path=path,
        content=content,
        exists=exists,
        mtime_ns=stat.st_mtime_ns if stat else None,
        size=stat.st_size if stat else None,
        sha256=sha256_file(path) if exists and path.is_file() else None,
        content_digest=content_digest,
        offset=offset,
        limit=limit,
        # is_partial_view 比 offset/limit 优先，是为了支持 summary/auto_inject 等非普通读取。
        view_kind="partial" if is_partial_view else ("range" if offset is not None or limit is not None else "full"),
        source=source,
        is_partial_view=is_partial_view,
        read_at=time.time(),
    )


def sha256_file(path: Path) -> str:
    """按块读取文件并计算 sha256，避免一次性加载超大文件。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def digest_text(text: str) -> str:
    """计算一段文本内容的 sha256 摘要。"""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _file_snapshot_to_dict(snapshot: FileSnapshot) -> dict[str, Any]:
    """把 FileSnapshot 转成可写入 JSON 的 dict。"""
    return {
        "path": str(snapshot.path),
        "content": snapshot.content,
        "exists": snapshot.exists,
        "mtime_ns": snapshot.mtime_ns,
        "size": snapshot.size,
        "sha256": snapshot.sha256,
        "content_digest": snapshot.content_digest,
        "offset": snapshot.offset,
        "limit": snapshot.limit,
        "view_kind": snapshot.view_kind,
        "source": snapshot.source,
        "is_partial_view": snapshot.is_partial_view,
        "read_at": snapshot.read_at,
    }


def _file_snapshot_from_dict(data: dict[str, Any]) -> FileSnapshot | None:
    """把 JSON dict 容错恢复成 FileSnapshot。"""
    try:
        return FileSnapshot(
            path=Path(str(data["path"])).resolve(strict=False),
            content=data.get("content") if isinstance(data.get("content"), str) else None,
            exists=bool(data.get("exists")),
            mtime_ns=_optional_int(data.get("mtime_ns")),
            size=_optional_int(data.get("size")),
            sha256=data.get("sha256") if isinstance(data.get("sha256"), str) else None,
            content_digest=data.get("content_digest") if isinstance(data.get("content_digest"), str) else None,
            offset=_optional_int(data.get("offset")),
            limit=_optional_int(data.get("limit")),
            view_kind=_coerce_view_kind(data.get("view_kind")),
            source=_coerce_source(data.get("source")),
            is_partial_view=bool(data.get("is_partial_view")),
            read_at=float(data.get("read_at") or 0.0),
        )
    except Exception:
        return None


def _optional_int(value: Any) -> int | None:
    """把值转成 int；None 或转换失败时返回 None。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_view_kind(value: Any) -> Literal["full", "range", "summary", "partial"]:
    """把未知 view_kind 降级为 partial。"""
    return value if value in {"full", "range", "summary", "partial"} else "partial"


def _coerce_source(value: Any) -> Literal["read", "write", "auto_inject", "summary"]:
    """把未知 snapshot source 降级为 read。"""
    return value if value in {"read", "write", "auto_inject", "summary"} else "read"
