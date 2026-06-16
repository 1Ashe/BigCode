"""config 子包的对外导出。

学习思路：外部只需要 load_runtime_config() 和几个配置 dataclass，不必直接依赖 loader.py/models.py 的文件结构。
"""

from .loader import load_runtime_config
from .models import CompactConfig, ModelCapabilities, ModelProtocol, ResolvedModel, RuntimeConfig

__all__ = ["CompactConfig", "ModelCapabilities", "ModelProtocol", "ResolvedModel", "RuntimeConfig", "load_runtime_config"]
