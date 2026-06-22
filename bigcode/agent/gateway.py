"""把 AgentEvent 转成机器可读结构。"""
from __future__ import annotations

from typing import Any

from bigcode.utils.jsonio import to_jsonable


EVENT_SCHEMA_VERSION = 1


def serialize_agent_event(event: Any) -> dict[str, Any]:
    """把内部对象转换成可写入 JSON 或传给外部系统的普通结构。"""
    payload = to_jsonable(event)
    if not isinstance(payload, dict):
        raise TypeError("Agent events must serialize to JSON objects.")
    return {"schema_version": EVENT_SCHEMA_VERSION, **payload}
