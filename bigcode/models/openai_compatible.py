"""旧名称兼容层。

学习思路：OpenAICompatibleModelClient 现在只是 ClaudeCompatibleModelClient 的别名，保留它是为了不破坏旧导入。
"""
from __future__ import annotations

from .claude_compatible import ClaudeCompatibleModelClient, ModelResponse


class OpenAICompatibleModelClient(ClaudeCompatibleModelClient):
    """Compatibility alias. BigCode now sends Claude Messages requests."""


__all__ = ["OpenAICompatibleModelClient", "ModelResponse"]
