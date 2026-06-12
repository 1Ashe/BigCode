"""ID 和安全文件名辅助函数。

学习思路：new_id 生成带前缀的短随机 id，project_id_for_path 用路径 hash 避免不同项目重名。
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from pathlib import Path


_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def new_id(prefix: str) -> str:
    """生成带前缀的短唯一 id。

    时间戳提供大致顺序，随机十六进制降低碰撞概率。
    """
    return f"{prefix}_{int(time.time() * 1000):x}_{secrets.token_hex(4)}"


def safe_slug(value: str, *, fallback: str = "session") -> str:
    """把任意字符串转换成适合作为文件名片段的安全 slug。"""
    value = _SAFE_RE.sub("-", value.strip()).strip("-")
    return value[:80] or fallback


def project_id_for_path(path: Path) -> str:
    """根据项目路径生成稳定项目 id。

    路径名便于人读，hash 用来区分同名但不同目录的项目。
    """
    resolved = str(path.resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return f"{safe_slug(path.name, fallback='project')}-{digest}"

