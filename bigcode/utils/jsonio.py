"""JSON/JSONL 读写和可序列化转换工具。

学习思路：项目状态大量写成 JSON 文件，这里统一处理 dataclass、Pydantic、Path 等类型。
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def read_json_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """读取 JSON 对象文件；不存在返回 (None, None)，错误返回错误字符串。"""
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, None
    except Exception as exc:
        return None, f"{path}: {exc}"
    if not isinstance(data, dict):
        return None, f"{path}: expected JSON object"
    return data, None


def write_json_file(path: Path, data: Any) -> None:
    """原子写入 JSON 文件：先写 .tmp，再 replace 到目标路径。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(to_jsonable(data), f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def append_jsonl(path: Path, data: Any) -> None:
    """向 JSONL 文件追加一行 JSON，并自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(to_jsonable(data), ensure_ascii=False, sort_keys=True))
        f.write("\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 文件，跳过空行、坏行和非对象行。"""
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def to_jsonable(value: Any) -> Any:
    """递归把 dataclass、Pydantic 模型、Path 等对象转换成 JSON 友好的值。"""
    if is_dataclass(value):
        return {k: to_jsonable(v) for k, v in asdict(value).items()}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """递归合并两个 dict。

    overlay 中的值优先；如果两边同一个键都是 dict，则继续深度合并。
    """
    result = dict(base)
    for key, value in overlay.items():
        if value is None:
            result[key] = None
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
