"""mcp 子包的对外导出。

学习思路：McpClientManager 是 MCP 能力的统一入口，具体工具包装在 bigcode/tools/mcp。
"""

from .client import McpClientManager

__all__ = ["McpClientManager"]
