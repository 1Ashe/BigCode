"""保存过大的工具结果，并提供按 id 读取的工具。

学习思路：ToolRunner 会把超大结果写成 artifact，只把 artifact_id 等元数据放回上下文，防止上下文爆掉。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from bigcode.utils.jsonio import to_jsonable, write_json_file

from bigcode.tools.base import BaseTool, PermissionDecision, ToolExecutionContext, ToolResult, ValidationResult
from bigcode.tools.output_limits import limit_text
from bigcode.tools.permissions import build_permission_target, check_content_policy, check_mode_policy_for_target


_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True)
class ArtifactRecord:
    """结构化结果数据。

    这种 dataclass 主要用于在模块之间传递信息，比直接使用 dict 更容易看出字段含义。
    """
    artifact_id: str
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
        artifact_id: str,
        tool_use_id: str,
        tool_name: str,
        output: Any,
        output_metadata: dict[str, Any] | None = None,
        run_metadata: dict[str, Any] | None = None,
        is_error: bool = False,
        error_message: str = "",
    ) -> ArtifactRecord:
        """把一次工具输出完整写入 artifact JSON 文件。"""
        artifact_id = _validate_artifact_id(artifact_id)

        # payload 保存的不只是 output 本身，也包含工具名、错误信息和 metadata。
        # 这样以后单独打开 artifact 文件时，仍能知道它来自哪次工具调用。
        payload = {
            "artifact_id": artifact_id,
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

        # _path_for 会再次校验 artifact_id，确保生成路径不会逃出当前 session 目录。
        path = self._path_for(artifact_id)
        write_json_file(path, payload)
        return ArtifactRecord(
            artifact_id=artifact_id,
            artifact_path=str(path),
            session_id=self.session_id,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            original_chars=original_chars,
            created_at=payload["created_at"],
        )

    def read_text(self, artifact_id: str, *, registered_artifacts: dict[str, dict[str, Any]], max_chars: int) -> dict[str, Any]:
        """读取当前会话登记过的 artifact 文本。"""
        artifact_id = _validate_artifact_id(artifact_id)
        record = registered_artifacts.get(artifact_id)
        if record is None:
            raise RuntimeError("Unknown artifact for current session.")
        path = self._path_for(artifact_id)
        registered_path = record.get("artifact_path")
        if registered_path and Path(str(registered_path)).resolve(strict=False) != path:
            # 这里用登记路径和当前计算路径做一次交叉校验，防止 snapshot 被篡改后读错文件。
            raise RuntimeError("Artifact path does not match current session store.")
        if not path.is_file():
            raise RuntimeError("Artifact file is missing.")
        text = path.read_text(encoding="utf-8", errors="replace")
        limited, truncated = limit_text(text, max_chars)
        return {
            "artifact_id": artifact_id,
            "artifact_path": str(path),
            "content": limited,
            "truncated": truncated,
            "original_chars": len(text),
        }

    def _path_for(self, artifact_id: str) -> Path:
        """计算 artifact 文件路径，并确认路径仍在当前 session 目录内。"""
        artifact_id = _validate_artifact_id(artifact_id)
        path = (self.root / f"{artifact_id}.json").resolve(strict=False)
        root = self.root.resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise RuntimeError("Artifact path escaped session store.") from exc
        return path


class ArtifactReadInput(BaseModel):
    """工具输入模型。

    Pydantic 会根据这些字段校验模型传进来的参数，字段类型就是最重要的阅读线索。
    """
    artifact_id: str
    max_chars: int = Field(default=100_000, ge=1, le=1_000_000)


class ArtifactReadTool(BaseTool[ArtifactReadInput, dict]):
    """一个可被模型调用的工具类。

    ToolRunner 会先校验 input_model 和权限，再调用这个类的 call() 方法执行真正逻辑。
    """
    name = "ArtifactRead"
    description = "Read a BigCode-managed tool result artifact for the current session."
    input_model = ArtifactReadInput
    permission_category = "read"
    state_effect = "none"
    max_result_chars = 1_100_000

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        return ctx.artifact_store is not None

    def is_concurrency_safe(self, input: ArtifactReadInput, ctx: ToolExecutionContext) -> bool:
        return True

    async def validate_input(self, input: ArtifactReadInput, ctx: ToolExecutionContext) -> ValidationResult:
        try:
            _validate_artifact_id(input.artifact_id)
        except RuntimeError as exc:
            return ValidationResult(False, str(exc))
        return ValidationResult(True)

    async def check_permissions(self, input: ArtifactReadInput, ctx: ToolExecutionContext) -> PermissionDecision:
        target = build_permission_target(self, input)
        decision = check_content_policy(target, ctx)
        if decision:
            return decision
        decision = check_mode_policy_for_target(target, ctx)
        if decision:
            return decision
        return PermissionDecision("allow", message="Current-session artifact read allowed.", updated_input=input)

    async def call(self, input: ArtifactReadInput, ctx: ToolExecutionContext, on_progress=None) -> ToolResult[dict]:
        """工具执行入口。

        input 是已经通过 Pydantic 校验的参数，ctx 提供 cwd、权限、会话状态等运行环境。
        """
        if ctx.artifact_store is None:
            raise RuntimeError("Artifact store is not configured.")
        if ctx.active_artifacts is None:
            raise RuntimeError("No current-session artifacts are registered.")
        return ToolResult(ctx.artifact_store.read_text(input.artifact_id, registered_artifacts=ctx.active_artifacts, max_chars=input.max_chars))


def serialized_chars(value: Any) -> int:
    """把内部对象转换成可写入 JSON 或传给外部系统的普通结构。"""
    return len(_json_dumps(to_jsonable(value)))


def _json_dumps(value: Any) -> str:
    """把值序列化成稳定 JSON 字符串，用于统计长度。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _validate_artifact_id(artifact_id: str) -> str:
    """校验 artifact id 只包含安全字符。"""
    if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise RuntimeError("Invalid artifact id.")
    return artifact_id
