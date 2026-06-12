"""工作区路径解析和越界判断。

学习思路：所有读写工具都应先 resolve_path，确认真实路径是否仍在 workspace_roots 内。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResolvedPath:
    """路径解析结果。

    同时保存用户请求路径、真实路径、父目录、是否存在以及是否在工作区内。
    """
    requested: Path
    resolved: Path
    exists: bool
    parent_resolved: Path
    inside_workspace: bool
    workspace_root: Path | None
    is_symlink_escape: bool


def resolve_path(requested: str | Path, cwd: Path, workspace_roots: list[Path], *, must_exist: bool = False) -> ResolvedPath:
    """把用户输入路径解析成真实路径，并判断它是否仍在工作区内。"""
    req = Path(requested)
    candidate = req if req.is_absolute() else cwd / req
    if candidate.exists():
        # 已存在路径用 strict=True 解析真实路径，能展开符号链接。
        resolved = candidate.resolve(strict=True)
        parent_resolved = resolved.parent
        exists = True
    else:
        if must_exist:
            raise FileNotFoundError(str(candidate))
        # 新文件路径无法 strict resolve 自己，但可以解析父目录后再拼回文件名。
        parent_resolved = candidate.parent.resolve(strict=True)
        resolved = parent_resolved / candidate.name
        exists = False
    root = workspace_root_for(resolved, workspace_roots)
    lexical_root = workspace_root_for(candidate.resolve(strict=False), workspace_roots)
    # lexical_root 有值但真实 root 没有，通常说明符号链接把路径带出了工作区。
    return ResolvedPath(
        requested=req,
        resolved=resolved,
        exists=exists,
        parent_resolved=parent_resolved,
        inside_workspace=root is not None,
        workspace_root=root,
        is_symlink_escape=lexical_root is not None and root is None,
    )


def workspace_root_for(path: Path, roots: list[Path]) -> Path | None:
    """返回包含某个路径的 workspace root；不在任何 root 内则返回 None。"""
    for root in roots:
        try:
            path.relative_to(root)
            return root
        except ValueError:
            continue
    return None
