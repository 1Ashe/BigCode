"""后台 subAgent 任务的状态和持久化。

学习思路：同步子代理直接返回结果，后台子代理会把状态 JSON、输出文本和 sidechain transcript 写入项目状态目录。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from bigcode.utils.jsonio import read_json_file, to_jsonable, write_json_file


TaskStatus = Literal["queued", "running", "completed", "failed", "cancelled"]

_SAFE_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class AgentRunResult:
    """结构化结果数据。

    这种 dataclass 主要用于在模块之间传递信息，比直接使用 dict 更容易看出字段含义。
    """
    agent_id: str
    agent_type: str
    content: str
    total_tool_use_count: int = 0
    total_duration_ms: int = 0
    total_tokens: int = 0
    stop_reason: str | None = None
    sidechain_transcript_path: str | None = None


@dataclass
class AgentTaskState:
    """运行时状态对象。

    字段主要记录当前流程走到哪里，通常会被会话或工具持续更新。
    """
    agent_id: str
    agent_type: str
    description: str
    prompt: str
    status: TaskStatus = "queued"
    output_file: str = ""
    sidechain_transcript_path: str = ""
    parent_session_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    duration_ms: int = 0
    total_tool_use_count: int = 0
    total_tokens: int = 0
    stop_reason: str | None = None


class AgentTaskStore:
    """本地状态存储封装。

    它把文件路径、读写和必要的并发保护集中起来，调用方不用关心磁盘细节。
    """
    def __init__(self, project_state_dir: Path) -> None:
        """保存项目状态目录，并派生后台任务、输出和 transcript 子目录。"""
        self.project_state_dir = project_state_dir
        self.tasks_dir = project_state_dir / "agent-tasks"
        self.outputs_dir = project_state_dir / "agent-task-outputs"
        self.transcripts_dir = project_state_dir / "subagents"

    def task_path(self, agent_id: str) -> Path:
        """计算某个后台子代理任务的状态 JSON 路径。"""
        agent_id = validate_agent_id(agent_id)
        return self.tasks_dir / f"{agent_id}.json"

    def output_path(self, agent_id: str) -> Path:
        """计算某个后台子代理任务的输出文本路径。"""
        agent_id = validate_agent_id(agent_id)
        return self.outputs_dir / f"{agent_id}.txt"

    def transcript_path(self, agent_id: str) -> Path:
        """计算某个后台子代理的 sidechain transcript 路径。"""
        agent_id = validate_agent_id(agent_id)
        return self.transcripts_dir / f"{agent_id}.jsonl"

    def create(
        self,
        *,
        agent_id: str,
        agent_type: str,
        description: str,
        prompt: str,
        parent_session_id: str | None,
    ) -> AgentTaskState:
        """创建 queued 状态的后台子代理任务，并立即写入磁盘。"""
        agent_id = validate_agent_id(agent_id)
        state = AgentTaskState(
            agent_id=agent_id,
            agent_type=agent_type,
            description=description,
            prompt=prompt,
            output_file=str(self.output_path(agent_id)),
            sidechain_transcript_path=str(self.transcript_path(agent_id)),
            parent_session_id=parent_session_id,
        )
        self.write_state(state)
        return state

    def write_state(self, state: AgentTaskState) -> None:
        """写入后台子代理任务状态 JSON，并刷新 updated_at。"""
        state.updated_at = time.time()
        write_json_file(self.task_path(state.agent_id), state)

    def read_state(self, agent_id: str) -> AgentTaskState | None:
        """读取后台子代理任务状态；文件缺失或损坏时返回 None。"""
        data, error = read_json_file(self.task_path(agent_id))
        if error or not data:
            return None
        return _state_from_dict(data)

    def list_states(self) -> list[AgentTaskState]:
        """列出所有后台子代理任务状态，跳过非法 id 或损坏文件。"""
        if not self.tasks_dir.exists():
            return []
        states: list[AgentTaskState] = []
        for path in self.tasks_dir.glob("*.json"):
            agent_id = path.stem
            if not is_valid_agent_id(agent_id):
                continue
            data, error = read_json_file(path)
            if error or not data:
                continue
            state = _state_from_dict(data)
            if state is not None:
                states.append(state)
        return sorted(states, key=lambda state: state.updated_at, reverse=True)

    def write_output(self, agent_id: str, content: str) -> Path:
        """写入后台子代理最终输出文本。"""
        path = self.output_path(agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def read_output(self, agent_id: str, *, max_chars: int) -> tuple[str, bool]:
        """读取后台子代理输出文本，并按 max_chars 截断。"""
        path = self.output_path(agent_id)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return "", False
        if max_chars < 0:
            max_chars = 0
        truncated = len(text) > max_chars
        return text[:max_chars], truncated

    def status_counts(self) -> dict[str, int]:
        """统计各类后台子代理任务状态数量。"""
        counts = {status: 0 for status in ("queued", "running", "completed", "failed", "cancelled")}
        for state in self.list_states():
            counts[state.status] = counts.get(state.status, 0) + 1
        return counts


def validate_agent_id(agent_id: str) -> str:
    """校验 agent_id 是否只包含安全字符；不合法时抛错。"""
    if not is_valid_agent_id(agent_id):
        raise RuntimeError(f"Invalid agent_id: {agent_id!r}")
    return agent_id


def is_valid_agent_id(agent_id: str) -> bool:
    """返回 agent_id 是否符合安全命名规则。"""
    return bool(agent_id and _SAFE_AGENT_ID_RE.fullmatch(agent_id))


def render_agent_result(result: AgentRunResult) -> str:
    """把结构化数据渲染成人类可读的字符串。"""
    content = result.content.rstrip()
    stats = [
        "",
        "Stats:",
        f"- agent_id: {result.agent_id}",
        f"- agent_type: {result.agent_type}",
        f"- tool uses: {result.total_tool_use_count}",
        f"- duration: {result.total_duration_ms} ms",
        f"- tokens: {result.total_tokens}",
    ]
    if result.stop_reason:
        stats.append(f"- stop_reason: {result.stop_reason}")
    return "\n".join([content, *stats]).lstrip()


def task_summary(state: AgentTaskState) -> dict[str, Any]:
    """把 AgentTaskState 精简成适合工具返回的摘要 dict。"""
    return {
        "agent_id": state.agent_id,
        "agent_type": state.agent_type,
        "description": state.description,
        "status": state.status,
        "output_file": state.output_file,
        "sidechain_transcript_path": state.sidechain_transcript_path,
        "parent_session_id": state.parent_session_id,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "started_at": state.started_at,
        "completed_at": state.completed_at,
        "duration_ms": state.duration_ms,
        "total_tool_use_count": state.total_tool_use_count,
        "total_tokens": state.total_tokens,
        "stop_reason": state.stop_reason,
        "error": state.error,
    }


def _state_from_dict(data: dict[str, Any]) -> AgentTaskState | None:
    """把后台子代理任务 JSON 容错转换成 AgentTaskState。"""
    try:
        # agent_id 会用于文件名，所以恢复时也要重新校验，防止坏文件进入列表。
        agent_id = validate_agent_id(str(data["agent_id"]))
    except Exception:
        return None
    status = str(data.get("status") or "queued")
    if status not in {"queued", "running", "completed", "failed", "cancelled"}:
        status = "queued"
    result = data.get("result")
    if not isinstance(result, dict):
        result = None

    # 所有字段都做宽松转换，是为了兼容旧版本状态文件或手工编辑过的 JSON。
    return AgentTaskState(
        agent_id=agent_id,
        agent_type=str(data.get("agent_type") or ""),
        description=str(data.get("description") or ""),
        prompt=str(data.get("prompt") or ""),
        status=status,  # type: ignore[arg-type]
        output_file=str(data.get("output_file") or ""),
        sidechain_transcript_path=str(data.get("sidechain_transcript_path") or ""),
        parent_session_id=str(data["parent_session_id"]) if data.get("parent_session_id") else None,
        result=result,
        error=str(data["error"]) if data.get("error") else None,
        created_at=_float_or_now(data.get("created_at")),
        updated_at=_float_or_zero(data.get("updated_at")),
        started_at=_float_or_none(data.get("started_at")),
        completed_at=_float_or_none(data.get("completed_at")),
        duration_ms=_int_or_zero(data.get("duration_ms")),
        total_tool_use_count=_int_or_zero(data.get("total_tool_use_count")),
        total_tokens=_int_or_zero(data.get("total_tokens")),
        stop_reason=str(data["stop_reason"]) if data.get("stop_reason") else None,
    )


def result_to_dict(result: AgentRunResult) -> dict[str, Any]:
    """把 AgentRunResult 转成 JSON 友好的 dict。"""
    return to_jsonable(result)


def _float_or_now(value: Any) -> float:
    """把值转成 float，失败时返回当前时间。"""
    parsed = _float_or_none(value)
    return parsed if parsed is not None else time.time()


def _float_or_zero(value: Any) -> float:
    """把值转成 float，失败时返回 0.0。"""
    parsed = _float_or_none(value)
    return parsed if parsed is not None else 0.0


def _float_or_none(value: Any) -> float | None:
    """把值转成 float，失败时返回 None。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    """把值转成 int，失败时返回 0。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
