"""Render AgentEvent streams to a terminal UI."""
from __future__ import annotations

from typing import Any

from .console import BigCodeTUI
from .prompt import erase_previous_prompt_line

try:
    from rich.status import Status
    from rich.text import Text
except ImportError:
    Status = None  # type: ignore[assignment]
    Text = None  # type: ignore[assignment]


class BigCodeStreamRenderer:
    """把 AgentEvent 流渲染成人类可读终端输出。"""

    def __init__(self, ui: BigCodeTUI) -> None:
        self.ui = ui
        self._stream_open = False
        self._status: Any | None = None
        self._answer_started = False
        self.assistant_text = ""

    def handle(self, event: Any) -> None:
        """消费单个 AgentEvent。"""
        event_type = getattr(event, "event_type", "")
        if event_type == "stream":
            if not self._answer_started:
                erase_previous_prompt_line(lines=1)
            self._clear_status()
            self.ui.stream_text(event.text)
            self.assistant_text += event.text
            self._stream_open = True
            self._answer_started = True
            return
        self._close_stream()
        if self._answer_started and event_type != "turn_completed":
            return
        if event_type == "status":
            self._handle_status(event)
            return
        if event_type == "permission_requested":
            self._clear_status()
            return
        if event_type == "permission_resolved":
            status = "approved" if event.approved else "denied"
            self._set_status(f"Permission {status}: {event.tool_name}")
            return
        if event_type == "tool_started":
            self._set_status(f"Running {event.tool_name}...")
            return
        if event_type == "tool_progress":
            if event.progress:
                self._set_status(f"Running {event.tool_name}: {event.progress}")
            return
        if event_type == "tool_completed":
            status = "failed" if event.is_error else "completed"
            self._set_status(f"{status.title()} {event.tool_name}")
            return
        if event_type == "error":
            self._set_status(f"Error: {event.message}")
            return
        if event_type == "turn_completed":
            self.close()

    def _close_stream(self) -> None:
        if self._stream_open:
            self.ui.print()
            self._stream_open = False

    def close(self) -> None:
        """关闭 Rich 状态/流式文本状态。"""
        self._close_stream()
        self._clear_status()

    def _handle_status(self, event: Any) -> None:
        status = getattr(event, "status", "")
        metadata = getattr(event, "metadata", {}) or {}
        if status == "turn_started":
            self._set_status("Thinking...")
        elif status == "model_request_started":
            self._set_status("Thinking...")
        elif status == "model_tool_call_started":
            self._set_status(f"Thinking... calling {metadata.get('tool_name', '(unknown)')}")
        elif status == "compact_started":
            self._set_status("Thinking... compacting context")
        elif status == "compact_completed":
            self._set_status("Thinking...")
        elif status == "context_compaction_blocked":
            self._set_status("Context compaction blocked")
        elif status == "turn_max_steps":
            self._set_status("Max steps reached")

    def _set_status(self, message: str) -> None:
        if not self.ui.enabled:
            return
        if self.ui.console is None or Status is None or Text is None:
            return
        if not message:
            return
        renderable = Text(message, style="dim")
        if self._status is None:
            self._status = Status(renderable, console=self.ui.console, spinner="dots")
            self._status.start()
        else:
            self._status.update(renderable)

    def _clear_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None
