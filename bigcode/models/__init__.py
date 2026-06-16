"""模型客户端子包的对外导出。

学习思路：新主路径使用 create_client() 创建 Anthropic/OpenAI SDK 流式客户端；
旧的 ClaudeCompatibleModelClient/OpenAICompatibleModelClient 继续保留兼容导入。
"""

from .client import AnthropicClient, LLMClient, OpenAIClient, create_client
from .claude_compatible import ClaudeCompatibleModelClient
from .events import (
    StreamEnd,
    StreamEvent,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)
from .openai_compatible import OpenAICompatibleModelClient

__all__ = [
    "AnthropicClient",
    "ClaudeCompatibleModelClient",
    "LLMClient",
    "OpenAIClient",
    "OpenAICompatibleModelClient",
    "StreamEnd",
    "StreamEvent",
    "TextDelta",
    "ThinkingComplete",
    "ThinkingDelta",
    "ToolCallComplete",
    "ToolCallDelta",
    "ToolCallStart",
    "create_client",
]
