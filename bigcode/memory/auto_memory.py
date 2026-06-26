"""Markdown-backed long-term memory storage and extraction."""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bigcode.context.messages import AssistantMessage, MessageBase, TextBlock, ToolUseBlock, UserMessage, text_from_blocks
from bigcode.utils.ids import safe_slug


USER_MEMORY_TYPES = {"user", "feedback"}
PROJECT_MEMORY_TYPES = {"project", "reference"}
ALL_MEMORY_TYPES = USER_MEMORY_TYPES | PROJECT_MEMORY_TYPES
INDEX_FILENAME = "MEMORY.md"
MAX_INDEX_LINES = 200
MAX_INDEX_CHARS = 25_000


MEMORY_EXTRACTION_SYSTEM = "You are a BigCode memory extraction helper. Output only JSON. Do not call tools."

MEMORY_EXTRACTION_PROMPT = """\
Analyze the current long-term memories and the recent conversation.

Decide whether to create, update, or delete memory files.

Memory types:
- user: user's personal coding style and durable preferences
- feedback: user's corrections and preferred fixes
- project: facts about this repository
- reference: external links and reference material

Rules:
- Do not create duplicates with the same meaning.
- Prefer updates over creating near-duplicates.
- Only store durable information likely to matter in future sessions.
- Use user/feedback for user-level memories and project/reference for project-level memories.
- If nothing is worth remembering, return {"actions":[]}.

Return strict JSON:
{
  "actions": [
    {
      "action": "create" | "update" | "delete",
      "type": "user" | "feedback" | "project" | "reference",
      "name": "stable-slug",
      "title": "short index title",
      "description": "one sentence",
      "body": "Markdown body for create/update, including Why/How when useful"
    }
  ]
}
"""


@dataclass(frozen=True)
class MemoryEntry:
    name: str
    type: str
    title: str
    description: str
    body: str
    path: Path


class MemoryManager:
    """Manage user-level and project-level Markdown memories."""

    def __init__(self, *, user_memory_dir: Path, project_memory_dir: Path) -> None:
        self.user_memory_dir = user_memory_dir
        self.project_memory_dir = project_memory_dir
        self._last_extraction_index = 0

    @classmethod
    def for_config(cls, config: Any) -> "MemoryManager":
        return cls(
            user_memory_dir=config.bigcode_home / "memory",
            project_memory_dir=config.repo_root / ".bigcode" / "memory",
        )

    def load_index_for_prompt(self) -> str:
        chunks: list[str] = []
        user_index = self._read_index(self.user_memory_dir)
        if user_index:
            chunks.append("## User Memories\n" + user_index)
        project_index = self._read_index(self.project_memory_dir)
        if project_index:
            chunks.append("## Project Memories\n" + project_index)
        content = "\n\n".join(chunks).strip()
        if not content:
            return ""
        return _truncate_index(content)

    async def extract(self, *, client: Any, protocol: str, messages: list[MessageBase]) -> None:
        recent = messages[self._last_extraction_index :]
        self._last_extraction_index = len(messages)
        conversation = _recent_conversation_text(recent)
        if not conversation:
            return

        prompt = (
            f"{MEMORY_EXTRACTION_PROMPT}\n\n"
            "## Existing Memories\n"
            f"{self._memory_inventory() or '(empty)'}\n\n"
            "## Recent Conversation\n"
            f"{conversation}\n"
        )
        collected = ""
        try:
            async for event in client.stream(
                MEMORY_EXTRACTION_SYSTEM,
                _prompt_messages(protocol, prompt),
                [],
            ):
                text = getattr(event, "text", None)
                if isinstance(text, str):
                    collected += text
        except Exception:
            return

        actions = _parse_actions(collected)
        if not actions:
            return
        self.apply_actions(actions)

    def apply_actions(self, actions: list[dict[str, Any]]) -> None:
        touched_dirs: set[Path] = set()
        for action in actions:
            typ = str(action.get("type") or "").strip()
            if typ not in ALL_MEMORY_TYPES:
                continue
            name = safe_slug(str(action.get("name") or action.get("title") or typ), fallback=typ)
            directory = self._dir_for_type(typ)
            path = directory / f"{name}.md"
            verb = str(action.get("action") or "").strip().lower()
            if verb == "delete":
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                touched_dirs.add(directory)
                continue
            if verb not in {"create", "update"}:
                continue
            title = str(action.get("title") or action.get("description") or name).strip()
            description = str(action.get("description") or title).strip()
            body = str(action.get("body") or description).strip()
            directory.mkdir(parents=True, exist_ok=True)
            path.write_text(_render_memory_file(name, typ, title, description, body), encoding="utf-8")
            touched_dirs.add(directory)

        for directory in touched_dirs:
            self._rewrite_index(directory)

    def _dir_for_type(self, typ: str) -> Path:
        return self.user_memory_dir if typ in USER_MEMORY_TYPES else self.project_memory_dir

    def _memory_inventory(self) -> str:
        entries = [*self._load_entries(self.user_memory_dir), *self._load_entries(self.project_memory_dir)]
        lines = []
        for entry in entries:
            scope = "user" if entry.type in USER_MEMORY_TYPES else "project"
            lines.append(f"- scope={scope} type={entry.type} name={entry.name}: {entry.description}")
        return "\n".join(lines)

    def _rewrite_index(self, directory: Path) -> None:
        entries = self._load_entries(directory)
        index = directory / INDEX_FILENAME
        if not entries:
            try:
                index.unlink()
            except FileNotFoundError:
                pass
            return
        lines = [
            f"- [{entry.title}]({entry.path.name}) - {entry.description}"
            for entry in sorted(entries, key=lambda item: (item.type, item.name))
        ]
        directory.mkdir(parents=True, exist_ok=True)
        index.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

    def _read_index(self, directory: Path) -> str:
        path = directory / INDEX_FILENAME
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""
        except OSError:
            return ""

    def _load_entries(self, directory: Path) -> list[MemoryEntry]:
        if not directory.exists():
            return []
        entries: list[MemoryEntry] = []
        for path in sorted(directory.glob("*.md")):
            if path.name == INDEX_FILENAME:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = _parse_frontmatter(text)
            name = safe_slug(str(meta.get("name") or path.stem), fallback=path.stem)
            typ = str(meta.get("type") or "").strip()
            if typ not in ALL_MEMORY_TYPES:
                continue
            description = str(meta.get("description") or "").strip()
            title = str(meta.get("title") or "").strip() or _title_from_description(description, name)
            entries.append(MemoryEntry(name=name, type=typ, title=title, description=description, body=body.strip(), path=path))
        return entries


def schedule_memory_extraction(manager: MemoryManager | None, *, client_factory: Any, protocol: str, messages: list[MessageBase]) -> asyncio.Task[None] | None:
    if manager is None:
        return None

    async def _run() -> None:
        await manager.extract(client=client_factory(), protocol=protocol, messages=list(messages))

    return asyncio.create_task(_run())


def _prompt_messages(protocol: str, prompt: str) -> list[Any]:
    if protocol == "openai":
        return [{"role": "user", "content": prompt}]
    from bigcode.context.messages import ApiMessage

    return [ApiMessage(role="user", content=[{"type": "text", "text": prompt}])]


def _recent_conversation_text(messages: list[MessageBase]) -> str:
    lines: list[str] = []
    for message in messages:
        if isinstance(message, UserMessage):
            text_parts = [block.text for block in message.content if isinstance(block, TextBlock)]
            if text_parts and not message.is_meta:
                lines.append("User: " + "\n".join(text_parts).strip())
        elif isinstance(message, AssistantMessage):
            text = text_from_blocks(message.content).strip()
            tool_names = [block.name for block in message.content if isinstance(block, ToolUseBlock)]
            if text:
                lines.append("Assistant: " + text)
            if tool_names:
                lines.append("Assistant tools: " + ", ".join(tool_names))
    return "\n\n".join(line for line in lines if line.strip())


def _parse_actions(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if not text:
        return []
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    actions = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(actions, list):
        return []
    return [item for item in actions if isinstance(item, dict)]


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip('"')
    return meta, body


def _render_memory_file(name: str, typ: str, title: str, description: str, body: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"title: {_yaml_scalar(title)}\n"
        f"description: {_yaml_scalar(description)}\n"
        f"type: {typ}\n"
        "---\n\n"
        f"{body.strip()}\n"
    )


def _yaml_scalar(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _title_from_description(description: str, fallback: str) -> str:
    if not description:
        return fallback
    title = description.strip().splitlines()[0]
    return title[:80]


def _truncate_index(content: str) -> str:
    lines = content.splitlines()
    truncated = False
    if len(lines) > MAX_INDEX_LINES:
        lines = lines[:MAX_INDEX_LINES]
        truncated = True
    content = "\n".join(lines)
    if len(content.encode("utf-8")) > MAX_INDEX_CHARS:
        raw = content.encode("utf-8")[:MAX_INDEX_CHARS]
        content = raw.decode("utf-8", errors="ignore")
        truncated = True
    if truncated:
        content = content.rstrip() + "\n\n[Long-term memory index truncated]"
    return content
