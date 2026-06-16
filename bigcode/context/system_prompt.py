"""在会话创建时生成一次固定的 system prompt。"""
from __future__ import annotations

import datetime as dt
import platform
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SystemPromptParts:
    """system prompt 的分段表示。

    static 是固定规则，dynamic 是当前环境信息，instructions 是从项目说明文件读取的内容。
    """
    static: str
    dynamic: str
    instructions: str = ""

    def render(self) -> str:
        """把 static、dynamic、instructions 三段拼成最终 system prompt 字符串。"""
        parts = [self.static.strip(), self.dynamic.strip()]
        if self.instructions.strip():
            parts.append("Project and user instructions:\n" + self.instructions.strip())
        return "\n\n".join(p for p in parts if p)


def build_system_prompt(
    *,
    cwd: Path,
    tool_names: list[str],
    instruction_paths: list[Path],
    role_instruction: str | None = None,
) -> SystemPromptParts:
    """生成会话级 prompt；调用方负责持久化并永久复用结果。"""
    # static 是无论何时都给模型看的固定行为准则。
    static = (
        "You are BigCode, a pragmatic coding agent. Use tools when they help. "
        "Do not invent file contents; inspect the workspace. Respect permissions and user instructions."
    )
    # 这些值只在会话创建时读取，之后即使环境变化也不重建。
    dynamic_lines = [
        f"Current date: {dt.date.today().isoformat()}",
        f"Platform: {platform.platform()}",
        f"cwd: {cwd}",
        "Available tools: " + ", ".join(sorted(tool_names)),
    ]
    if role_instruction:
        dynamic_lines.append("Session role:\n" + role_instruction.strip())
    instructions = _read_instructions(instruction_paths)
    return SystemPromptParts(static=static, dynamic="\n".join(dynamic_lines), instructions=instructions)


def _read_instructions(paths: list[Path], max_chars: int = 40000) -> str:
    """按顺序读取存在的说明文件，并限制总字符数。"""
    chunks = []
    remaining = max_chars
    for path in paths:
        if remaining <= 0 or not path.exists() or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > remaining:
            text = text[:remaining] + "\n[truncated]"
        chunks.append(f"## {path}\n{text}")
        remaining -= len(text)
    return "\n\n".join(chunks)
