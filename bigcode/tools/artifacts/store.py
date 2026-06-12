"""保存过大的工具结果。

学习思路：ToolRunner 会把超大结果写成 artifact，并把 artifact_path 放回上下文。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bigcode.utils.jsonio import to_jsonable, write_json_file


_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True)
class ArtifactRecord:
    """结构化结果数据。

    这种 dataclass 主要用于在模块之间传递信息，比直接使用 dict 更容易看出字段含义。
    """
    artifact_path: str
    session_id: str
    tool_use_id: str
    tool_name: str
    original_chars: int
    created_at: float


class ArtifactStore:
    """本地状态存储封装。

    它把文件路径、读写和必要的并发保护集中起来，调用方不用关心磁盘细节。
    """
    def __init__(self, project_state_dir: Path, session_id: str) -> None:
        """保存 artifact 根目录。

        同一 session 的大工具结果都会写到 tool-results/<session_id>/ 下。
        """
        self.project_state_dir = project_state_dir
        self.session_id = session_id
        self.root = project_state_dir / "tool-results" / session_id

    def write_tool_output(
        self,
        *,
        tool_use_id: str,
        tool_name: str,
        output: Any,
        output_metadata: dict[str, Any] | None = None,
        run_metadata: dict[str, Any] | None = None,
        is_error: bool = False,
        error_message: str = "",
    ) -> ArtifactRecord:
        """把一次工具输出完整写入 artifact JSON 文件。"""
        artifact_name = _validate_artifact_name(tool_use_id)

        # payload 保存的不只是 output 本身，也包含工具名、错误信息和 metadata。
        # 这样以后单独打开 artifact 文件时，仍能知道它来自哪次工具调用。
        payload = {
            "session_id": self.session_id,
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "created_at": time.time(),
            "output": to_jsonable(output),
            "output_metadata": to_jsonable(output_metadata or {}),
            "run_metadata": to_jsonable(run_metadata or {}),
            "is_error": is_error,
            "error_message": error_message,
        }
        original_chars = len(_json_dumps(payload["output"]))
        payload["original_chars"] = original_chars

        # _path_for 会再次校验文件名，确保生成路径不会逃出当前 session 目录。
        path = self._path_for(artifact_name)
        write_json_file(path, payload)
        return ArtifactRecord(
            artifact_path=str(path),
            session_id=self.session_id,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            original_chars=original_chars,
            created_at=payload["created_at"],
        )

    def _path_for(self, artifact_name: str) -> Path:
        """计算 artifact 文件路径，并确认路径仍在当前 session 目录内。"""
        artifact_name = _validate_artifact_name(artifact_name)
        path = (self.root / f"{artifact_name}.json").resolve(strict=False)
        root = self.root.resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise RuntimeError("Artifact path escaped session store.") from exc
        return path


def serialized_chars(value: Any) -> int:
    """把内部对象转换成可写入 JSON 或传给外部系统的普通结构。"""
    return len(_json_dumps(to_jsonable(value)))


def _json_dumps(value: Any) -> str:
    """把值序列化成稳定 JSON 字符串，用于统计长度。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _validate_artifact_name(artifact_name: str) -> str:
    """校验 artifact 文件名只包含安全字符。"""
    if not _ARTIFACT_ID_RE.fullmatch(artifact_name):
        raise RuntimeError("Invalid artifact name.")
    return artifact_name
