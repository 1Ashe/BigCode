"""定义可注入上下文的附加信息。

学习思路：Hook 或系统提醒会先变成 Attachment，再包装成模型可见的 system-reminder 文本。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Attachment:
    """待注入模型上下文的附件。

    Hook 可以返回 Attachment，让当前任务、计划模式提醒或能力索引进入下一次模型请求。
    """
    type: str
    text: str
    source: str = "system"
    metadata: dict[str, Any] = field(default_factory=dict)


def wrap_system_reminder(text: str) -> str:
    """把系统提醒包进 <system-reminder> 标签。

    这样模型能区分这类内容不是用户原始输入。
    """
    return f"<system-reminder>\n{text.strip()}\n</system-reminder>"

