"""保存和恢复会话快照。

学习思路：快照只保存“恢复会话需要的状态摘要”，完整对话内容保存在 transcript 的 JSONL 文件里。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bigcode.utils.jsonio import read_json_file, read_jsonl, to_jsonable, write_json_file


SNAPSHOT_VERSION = 1


@dataclass
class SessionSnapshot:
    """可持久化的会话快照。

    它保存恢复会话需要的摘要状态，不直接保存完整消息正文。
    """
    session_id: str
    cwd: str
    repo_root: str
    model: str | None
    permission_mode: str
    task_list_id: str
    transcript_path: str
    message_count: int
    read_file_snapshots: list[dict[str, Any]] = field(default_factory=list)
    loaded_skills: list[str] = field(default_factory=list)
    active_artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_verification: dict[str, Any] | None = None
    updated_at: float = field(default_factory=time.time)
    version: int = SNAPSHOT_VERSION


@dataclass(frozen=True)
class SessionListItem:
    """结构化结果数据。

    这种 dataclass 主要用于在模块之间传递信息，比直接使用 dict 更容易看出字段含义。
    """
    session_id: str
    cwd: str
    model: str | None
    message_count: int
    artifact_count: int
    updated_at: float
    transcript_path: str
    snapshot_path: str | None = None


def session_snapshot_path(project_state_dir: Path, session_id: str) -> Path:
    """根据项目状态目录和 session_id 计算快照文件路径。"""
    return project_state_dir / "sessions" / f"{session_id}.json"


def save_session_snapshot(project_state_dir: Path, snapshot: SessionSnapshot) -> Path:
    """把 SessionSnapshot 写到 sessions/<session_id>.json。"""
    path = session_snapshot_path(project_state_dir, snapshot.session_id)
    data = to_jsonable(snapshot)
    data["updated_at"] = time.time()
    write_json_file(path, data)
    return path


def load_session_snapshot(project_state_dir: Path, session_id: str) -> SessionSnapshot | None:
    """从磁盘或配置中加载数据，并转换成项目内部结构。"""
    data, error = read_json_file(session_snapshot_path(project_state_dir, session_id))
    if error or not data:
        return None
    return _snapshot_from_dict(data)


def list_session_snapshots(project_state_dir: Path) -> list[SessionListItem]:
    """列出可恢复会话，优先读取 snapshot，必要时回退到 transcript。"""
    items: dict[str, SessionListItem] = {}
    sessions_dir = project_state_dir / "sessions"
    if sessions_dir.exists():
        # 新版本会话都会有 snapshot，里面包含 cwd/model/artifact 等摘要。
        for path in sessions_dir.glob("*.json"):
            data, error = read_json_file(path)
            if error or not data:
                continue
            snapshot = _snapshot_from_dict(data)
            if snapshot is None:
                continue
            items[snapshot.session_id] = SessionListItem(
                session_id=snapshot.session_id,
                cwd=snapshot.cwd,
                model=snapshot.model,
                message_count=snapshot.message_count,
                artifact_count=len(snapshot.active_artifacts),
                updated_at=snapshot.updated_at,
                transcript_path=snapshot.transcript_path,
                snapshot_path=str(path),
            )

    transcripts_dir = project_state_dir / "transcripts"
    if transcripts_dir.exists():
        # 旧会话或异常情况下可能只有 transcript 没有 snapshot。
        # 这里仍然列出来，让用户可以看到并尝试恢复。
        for path in transcripts_dir.glob("*.jsonl"):
            session_id = path.stem
            if session_id in items:
                continue
            try:
                updated_at = path.stat().st_mtime
            except OSError:
                updated_at = 0.0
            items[session_id] = SessionListItem(
                session_id=session_id,
                cwd="",
                model=None,
                message_count=len(read_jsonl(path)),
                artifact_count=0,
                updated_at=updated_at,
                transcript_path=str(path),
                snapshot_path=None,
            )
    return sorted(items.values(), key=lambda item: item.updated_at, reverse=True)


def _snapshot_from_dict(data: dict[str, Any]) -> SessionSnapshot | None:
    """把 JSON dict 容错转换为 SessionSnapshot。"""
    try:
        # session_id 是最小必需字段；没有它就无法确定这是谁的快照。
        session_id = str(data["session_id"])
    except Exception:
        return None

    # 下面几个字段历史上可能不存在或类型不对，统一降级为空结构。
    active_artifacts = data.get("active_artifacts") or {}
    if not isinstance(active_artifacts, dict):
        active_artifacts = {}
    read_file_snapshots = data.get("read_file_snapshots") or []
    if not isinstance(read_file_snapshots, list):
        read_file_snapshots = []
    loaded_skills = data.get("loaded_skills") or []
    if not isinstance(loaded_skills, list):
        loaded_skills = []
    last_verification = data.get("last_verification")
    if not isinstance(last_verification, dict):
        last_verification = None
    return SessionSnapshot(
        session_id=session_id,
        cwd=str(data.get("cwd") or ""),
        repo_root=str(data.get("repo_root") or ""),
        model=str(data["model"]) if data.get("model") else None,
        permission_mode=str(data.get("permission_mode") or "default"),
        task_list_id=str(data.get("task_list_id") or session_id),
        transcript_path=str(data.get("transcript_path") or ""),
        message_count=_int_or_zero(data.get("message_count")),
        read_file_snapshots=[item for item in read_file_snapshots if isinstance(item, dict)],
        loaded_skills=[str(item) for item in loaded_skills if isinstance(item, str)],
        active_artifacts={str(key): value for key, value in active_artifacts.items() if isinstance(value, dict)},
        last_verification=last_verification,
        updated_at=float(data.get("updated_at") or 0.0),
        version=_int_or_zero(data.get("version")) or SNAPSHOT_VERSION,
    )


def _int_or_zero(value: Any) -> int:
    """把任意值转成 int，失败时返回 0。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
