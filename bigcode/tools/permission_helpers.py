"""向后兼容 re-export 模块。委托到 bigcode.tools.permissions.helpers。"""
from __future__ import annotations

from bigcode.tools.permissions.helpers import allow_with_mode_policy  # noqa: F401
