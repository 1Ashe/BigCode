"""技能扫描和注册逻辑。

学习思路：它支持两种来源：普通 skills/xxx/SKILL.md，以及插件 manifest 中声明的技能。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bigcode.utils.jsonio import read_json_file

from .models import SkillDefinition, SkillLoadReport


SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
BUILTIN_SKILL_ROOT = Path(__file__).resolve().parent / "builtin"


class SkillRegistry:
    """内存中的技能注册表，同时保存加载报告和错误。"""
    def __init__(self) -> None:
        """初始化空技能表、错误列表和加载报告列表。"""
        self._skills: dict[str, SkillDefinition] = {}
        self.errors: list[str] = []
        self.reports: list[SkillLoadReport] = []

    def add(self, skill: SkillDefinition) -> None:
        """注册一个可用技能，并记录 enabled 报告。"""
        self._skills[skill.name] = skill
        self.reports.append(
            SkillLoadReport(
                name=skill.name,
                status="enabled",
                source=skill.source,
                path=str(skill.skill_md),
                plugin_name=skill.plugin_name,
            )
        )

    def add_report(self, report: SkillLoadReport) -> None:
        """保存 disabled/failed 报告；失败报告还会进入 errors。"""
        self.reports.append(report)
        if report.status == "failed":
            self.errors.append(report.reason or f"failed to load skill {report.name!r}")

    def get(self, name: str) -> SkillDefinition | None:
        """按技能名查找 SkillDefinition。"""
        return self._skills.get(name)

    def list(self) -> list[SkillDefinition]:
        """返回当前已启用的所有技能。"""
        return list(self._skills.values())

    def capabilities(self) -> list[str]:
        """生成给模型看的技能能力摘要。"""
        return [f"Skill {s.name}: {s.description or 'load with SkillLoad'}" for s in self.list()]

    def status_counts(self) -> dict[str, int]:
        """统计 enabled/disabled/failed 技能加载状态。"""
        counts = {"enabled": 0, "disabled": 0, "failed": 0}
        for report in self.reports:
            counts[report.status] += 1
        return counts


def load_skills(roots: list[Path], *, max_skill_md_chars: int = 20000, include_builtin: bool = True) -> SkillRegistry:
    """扫描所有技能根目录，加载内置技能、插件技能和普通 SKILL.md。"""
    registry = SkillRegistry()
    scan_roots = ([BUILTIN_SKILL_ROOT] if include_builtin else []) + list(roots)
    for root in scan_roots:
        if not root.exists() or not root.is_dir():
            continue
        _load_plugin_manifests(root, registry, max_skill_md_chars=max_skill_md_chars)
        _load_legacy_skills(root, registry, max_skill_md_chars=max_skill_md_chars)
    return registry


def _load_plugin_manifests(root: Path, registry: SkillRegistry, *, max_skill_md_chars: int) -> None:
    """扫描插件 manifest，并按 manifest 中的 skills 配置加载技能。"""
    for manifest_path in sorted(root.glob("*/.codex-plugin/plugin.json")):
        plugin_root = manifest_path.parent.parent
        manifest, error = read_json_file(manifest_path)
        if error or not manifest:
            # manifest 读不出来时仍记录 report，doctor 命令才能告诉用户是哪一个插件坏了。
            registry.add_report(_failed_report(str(plugin_root.name), "plugin", manifest_path, error or "plugin manifest is empty"))
            continue
        plugin_name = _string_value(manifest.get("name")) or plugin_root.name
        if not _is_enabled(manifest):
            # 插件整体禁用时，不再扫描它下面的技能，但保留 disabled 报告。
            registry.add_report(
                SkillLoadReport(
                    name=plugin_name,
                    status="disabled",
                    source="plugin",
                    path=str(manifest_path),
                    reason="plugin disabled by manifest",
                    plugin_name=plugin_name,
                )
            )
            continue
        skills = manifest.get("skills") or []
        if not isinstance(skills, list):
            registry.add_report(_failed_report(plugin_name, "plugin", manifest_path, "plugin skills must be a list", plugin_name=plugin_name))
            continue
        for index, entry in enumerate(skills):
            # manifest.skills 里的每个 entry 描述一个技能：name、path、description、enabled。
            # 解析失败只跳过当前 entry，不影响同一个插件里的其它技能。
            if not isinstance(entry, dict):
                registry.add_report(_failed_report(f"{plugin_name}:skill-{index}", "plugin", manifest_path, "skill entry must be an object", plugin_name=plugin_name))
                continue
            if not _is_enabled(entry):
                disabled_name = _string_value(entry.get("name")) or f"{plugin_name}:skill-{index}"
                registry.add_report(
                    SkillLoadReport(
                        name=disabled_name,
                        status="disabled",
                        source="plugin",
                        path=str(manifest_path),
                        reason="skill disabled by manifest",
                        plugin_name=plugin_name,
                    )
                )
                continue
            rel_path = _string_value(entry.get("path")) or "SKILL.md"
            try:
                # path 必须留在 plugin_root 内，防止插件 manifest 读取任意系统文件。
                skill_md = _resolve_manifest_path(plugin_root, rel_path)
            except Exception as exc:
                registry.add_report(_failed_report(_string_value(entry.get("name")) or plugin_name, "plugin", manifest_path, str(exc), plugin_name=plugin_name))
                continue
            _load_skill_file(
                skill_md,
                registry,
                max_skill_md_chars=max_skill_md_chars,
                name_override=_string_value(entry.get("name")),
                description_override=_string_value(entry.get("description")),
                source="plugin",
                plugin_name=plugin_name,
            )


def _load_legacy_skills(root: Path, registry: SkillRegistry, *, max_skill_md_chars: int) -> None:
    """扫描传统目录结构 skills/<name>/SKILL.md。"""
    for skill_md in sorted(root.glob("*/SKILL.md")):
        if (skill_md.parent / ".codex-plugin" / "plugin.json").exists():
            continue
        _load_skill_file(skill_md, registry, max_skill_md_chars=max_skill_md_chars, source="skill")


def _load_skill_file(
    skill_md: Path,
    registry: SkillRegistry,
    *,
    max_skill_md_chars: int,
    name_override: str | None = None,
    description_override: str | None = None,
    source: str,
    plugin_name: str | None = None,
) -> None:
    """读取并校验单个 SKILL.md，提取名称、描述和资源列表后注册。"""
    try:
        skill_root = skill_md.parent.resolve(strict=True)
        real_md = skill_md.resolve(strict=True)
        real_md.relative_to(skill_root)

        # SKILL.md 可能比较大，注册阶段只读取前 max_skill_md_chars 用来提取元信息。
        # 真正 SkillLoad 时也会再次按 max_chars 控制返回长度。
        text = real_md.read_text(encoding="utf-8", errors="replace")[:max_skill_md_chars]
        name = name_override or _frontmatter_value(text, "name") or skill_root.name
        if not SKILL_NAME_RE.match(name):
            registry.add_report(_failed_report(name, source, real_md, f"invalid skill name {name!r} in {skill_md}", plugin_name=plugin_name))
            return
        description = description_override or _frontmatter_value(text, "description") or _first_nonempty_line(text)

        # resources 是技能目录里的附加文件清单。这里不读取内容，只列出路径；
        # 需要时模型再调用 SkillResourceRead 读取具体资源。
        resources = [
            str(path.relative_to(skill_root))
            for path in skill_root.rglob("*")
            if path.is_file() and path.name != "SKILL.md" and _safe_child(skill_root, path)
        ][:200]
        registry.add(
            SkillDefinition(
                name=name,
                root=skill_root,
                skill_md=real_md,
                description=description,
                resources=resources,
                source=source,
                plugin_name=plugin_name,
            )
        )
    except Exception as exc:
        registry.add_report(_failed_report(name_override or skill_md.parent.name, source, skill_md, f"{skill_md}: {exc}", plugin_name=plugin_name))


def _resolve_manifest_path(plugin_root: Path, rel_path: str) -> Path:
    """把插件 manifest 中的相对路径解析成合法的 SKILL.md。"""
    requested = Path(rel_path)
    if requested.is_absolute() or ".." in requested.parts:
        raise RuntimeError("skill path must be relative and stay inside plugin root")
    candidate = (plugin_root / requested).resolve(strict=True)
    candidate.relative_to(plugin_root.resolve(strict=True))
    if candidate.is_dir():
        candidate = (candidate / "SKILL.md").resolve(strict=True)
        candidate.relative_to(plugin_root.resolve(strict=True))
    if not candidate.is_file() or candidate.name != "SKILL.md":
        raise RuntimeError("skill path must point to a SKILL.md file")
    return candidate


def _frontmatter_value(text: str, key: str) -> str | None:
    """从 Markdown frontmatter 中取一个键值。"""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    for line in text[3:end].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip() == key:
                return v.strip().strip("'\"") or None
    return None


def _first_nonempty_line(text: str) -> str:
    """没有 description 时，用文档第一行非空内容作为技能描述。"""
    for line in text.splitlines():
        line = line.strip("# ").strip()
        if line and line != "---" and ":" not in line:
            return line[:160]
    return ""


def _safe_child(root: Path, path: Path) -> bool:
    """确认资源文件真实路径仍在技能目录内部。"""
    try:
        path.resolve(strict=True).relative_to(root)
        return True
    except Exception:
        return False


def _is_enabled(data: dict[str, Any]) -> bool:
    """读取 manifest 中的 enabled 标志，默认启用。"""
    return bool(data.get("enabled", True))


def _string_value(value: Any) -> str | None:
    """把非空字符串保留下来，其它类型当作缺失。"""
    return value if isinstance(value, str) and value.strip() else None


def _failed_report(name: str, source: str, path: Path, reason: str, *, plugin_name: str | None = None) -> SkillLoadReport:
    """构造一条加载失败报告。"""
    return SkillLoadReport(name=name, status="failed", source=source, path=str(path), reason=reason, plugin_name=plugin_name)
