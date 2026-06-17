"""context 子包的常用消息和附件类型导出。

学习思路：上下文相关代码会围绕 MessageBase、ContentBlock 和 Attachment 这些概念展开。
"""

from .attachments import Attachment, wrap_system_reminder
from .messages import (
    ApiMessage,
    AssistantMessage,
    AttachmentMessage,
    CompactRecordMessage,
    ContextSummaryMessage,
    MessageBase,
    SystemMessage,
    SystemPromptSnapshotMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

__all__ = [
    "ApiMessage",
    "AssistantMessage",
    "Attachment",
    "AttachmentMessage",
    "CompactRecordMessage",
    "ContextSummaryMessage",
    "MessageBase",
    "SystemMessage",
    "SystemPromptSnapshotMessage",
    "TextBlock",
    "ThinkingBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "UserMessage",
    "wrap_system_reminder",
]
