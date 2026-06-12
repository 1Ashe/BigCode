"""模型客户端子包的对外导出。

学习思路：当前实际发送的是 Claude Messages 兼容请求，OpenAICompatibleModelClient 只是兼容旧名称。
"""

from .claude_compatible import ClaudeCompatibleModelClient
from .openai_compatible import OpenAICompatibleModelClient

__all__ = ["ClaudeCompatibleModelClient", "OpenAICompatibleModelClient"]
