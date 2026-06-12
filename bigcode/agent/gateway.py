"""把 AgentEvent 转成机器可读的 JSONL 输出。

学习思路：普通交互模式直接 print 文本；event_stream=jsonl 时会走这里，便于外部程序消费事件。
"""
from __future__ import annotations

import json
from typing import Any

from bigcode.utils.jsonio import to_jsonable


EVENT_SCHEMA_VERSION = 1


def serialize_agent_event(event: Any) -> dict[str, Any]:
    """把内部对象转换成可写入 JSON 或传给外部系统的普通结构。"""
    payload = to_jsonable(event)
    if not isinstance(payload, dict):
        raise TypeError("Agent events must serialize to JSON objects.")
    return {"schema_version": EVENT_SCHEMA_VERSION, **payload}


class JsonlEventSink:
    """JSONL 事件输出器。

    它实现 __call__，所以可以像函数一样传给 AgentSession 的 event_sink。
    """
    def __init__(self, stream: Any) -> None:
        """保存输出流对象。

        stream 只要有 write() 方法即可，比如 sys.stdout 或测试里的 StringIO。
        """
        self.stream = stream

    def __call__(self, event: Any) -> None:
        """把一个事件序列化为一行 JSON，并写入输出流。"""
        self.stream.write(json.dumps(serialize_agent_event(event), ensure_ascii=False, sort_keys=True))
        self.stream.write("\n")
        flush = getattr(self.stream, "flush", None)
        if callable(flush):
            flush()
