"""hooks 子包的对外导出。

学习思路：HookBus 负责事件分发，HookInput/HookOutput/HookAggregate 是 hook 之间传递的数据结构。
"""

from .bus import HookBus
from .models import HookAggregate, HookInput, HookOutput

__all__ = ["HookAggregate", "HookBus", "HookInput", "HookOutput"]
