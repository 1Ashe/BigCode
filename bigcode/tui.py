"""BigCode 终端输出封装。

正常安装依赖后使用 Rich 管理终端页面；测试环境缺 Rich 时退回普通文本输出。
"""
from __future__ import annotations

from typing import Any

try:
    from rich.console import Console
    from rich.panel import Panel
except ImportError:
    Console = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]


class BigCodeTUI:
    """集中管理人类可读终端输出。"""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.console = Console() if Console is not None else None

    def print(self, *objects: Any, end: str = "\n") -> None:
        """输出普通内容。"""
        if not self.enabled:
            return
        if self.console is not None:
            self.console.print(*objects, end=end)
        else:
            print(*objects, end=end)

    def stream_text(self, text: str) -> None:
        """输出模型文本增量。"""
        if not self.enabled or not text:
            return
        if self.console is not None:
            self.console.print(text, end="", markup=False, highlight=False)
        else:
            print(text, end="", flush=True)

    def header(self, session_id: str, model: str | None) -> None:
        """输出会话头部。"""
        if not self.enabled:
            return
        title = f"BigCode session {session_id}"
        subtitle = f"model: {model or '(未配置)'}"
        if self.console is not None and Panel is not None:
            self.console.print(Panel(subtitle, title=title, border_style="cyan"))
        else:
            print(title)
            print(subtitle)

    def warning(self, message: str) -> None:
        """输出 warning。"""
        if self.console is not None:
            self.print(f"[yellow]{message}[/yellow]")
        else:
            self.print(f"Warning: {message}")

    def error(self, message: str) -> None:
        """输出错误。"""
        if self.console is not None:
            self.print(f"[red]{message}[/red]")
        else:
            self.print(f"Error: {message}")

    def tool_call(self, name: str, tool_id: str) -> None:
        """输出模型准备调用工具的提示。"""
        if self.console is not None:
            self.print(f"\n[bold cyan]工具[/bold cyan] {name} [dim]{tool_id}[/dim]")
        else:
            self.print(f"\n[tool] {name} ({tool_id})")

    def status_table(self, rows: dict[str, Any]) -> None:
        """输出 /status 使用的键值列表。"""
        for key, value in rows.items():
            self.print(f"{key}: {value}")


class BigCodeStreamRenderer:
    """把 AgentEvent 流渲染成人类可读终端输出。"""

    def __init__(self, ui: BigCodeTUI) -> None:
        self.ui = ui
        self._stream_open = False
        self.assistant_text = ""

    def handle(self, event: Any) -> None:
        """消费单个 AgentEvent。"""
        event_type = getattr(event, "event_type", "")
        if event_type == "stream":
            self.ui.stream_text(event.text)
            self.assistant_text += event.text
            self._stream_open = True
            return
        if event_type == "tool_started":
            self._close_stream()
            self.ui.tool_call(event.tool_name, event.tool_use_id)
            return
        if event_type == "tool_progress":
            self._close_stream()
            if event.progress:
                self.ui.print(f"[progress] {event.tool_name}: {event.progress}")
            return
        if event_type == "tool_completed":
            self._close_stream()
            status = "failed" if event.is_error else "completed"
            self.ui.print(f"[tool {status}] {event.tool_name} ({event.duration_ms} ms)")
            return
        if event_type == "error":
            self._close_stream()
            self.ui.error(event.message)
            return
        if event_type == "status":
            return
        if event_type == "turn_completed":
            self._close_stream()

    def _close_stream(self) -> None:
        if self._stream_open:
            self.ui.print()
            self._stream_open = False
