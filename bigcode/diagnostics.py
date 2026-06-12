"""实现 bigcode doctor 诊断命令。

学习思路：它逐项检查配置、工作区、模型、工具、技能、hook、MCP 和 provider 探测结果，并渲染成人能读的报告。
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from bigcode.config.models import ResolvedModel, RuntimeConfig
from bigcode.hooks.command import CommandRegistry
from bigcode.mcp import McpClientManager
from bigcode.skills import SkillRegistry, load_skills
from bigcode.tools import ToolRegistry, build_default_registry


DiagnosticStatus = Literal["OK", "WARN", "ERROR"]


@dataclass(frozen=True)
class DiagnosticItem:
    """结构化结果数据。

    这种 dataclass 主要用于在模块之间传递信息，比直接使用 dict 更容易看出字段含义。
    """
    status: DiagnosticStatus
    category: str
    name: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorReport:
    """结构化结果数据。

    这种 dataclass 主要用于在模块之间传递信息，比直接使用 dict 更容易看出字段含义。
    """
    items: list[DiagnosticItem] = field(default_factory=list)
    probe_enabled: bool = True
    active_model_ref: str | None = None

    @property
    def has_errors(self) -> bool:
        """判断诊断项中是否存在 ERROR。"""
        return any(item.status == "ERROR" for item in self.items)

    @property
    def has_warnings(self) -> bool:
        """判断诊断项中是否存在 WARN。"""
        return any(item.status == "WARN" for item in self.items)

    @property
    def overall_status(self) -> DiagnosticStatus:
        """根据所有诊断项汇总整体状态，ERROR 优先级高于 WARN。"""
        if self.has_errors:
            return "ERROR"
        if self.has_warnings:
            return "WARN"
        return "OK"

    def add(self, status: DiagnosticStatus, category: str, name: str, message: str, **details: Any) -> None:
        """向报告中追加一条诊断项。

        额外关键字参数会进入 details，渲染报告时会一起展示。
        """
        self.items.append(DiagnosticItem(status, category, name, message, details))


async def build_doctor_report(
    config: RuntimeConfig,
    *,
    model_ref: str | None = None,
    probe: bool = True,
    timeout: float = 10.0,
    env: dict[str, str] | None = None,
    registry: ToolRegistry | None = None,
    skill_registry: SkillRegistry | None = None,
    mcp_manager: McpClientManager | None = None,
) -> DoctorReport:
    """按固定顺序运行所有 doctor 检查，并返回结构化报告。"""
    env = env or os.environ
    active_model_ref = model_ref or config.default_model_ref
    report = DoctorReport(probe_enabled=probe, active_model_ref=active_model_ref)

    # 这些检查大多只读本地配置和内存状态；MCP/provider probe 可能发起外部请求。
    # 每个检查函数只负责往 report 里追加 DiagnosticItem。
    _check_config(config, report)
    _check_workspace(config, report)
    active_model = _check_models(config, active_model_ref, report, env)
    _check_tools(report, registry)
    _check_skills(config, report, skill_registry)
    _check_commands(config, report)
    await _check_mcp(config, report, probe=probe, mcp_manager=mcp_manager)
    await _check_provider_probe(active_model, report, probe=probe, timeout=timeout, env=env)
    return report


def render_doctor_report(report: DoctorReport) -> str:
    """把结构化数据渲染成人类可读的字符串。"""
    # 报告头部先给整体结论，后面再按 category 展开细节。
    lines = [
        "BigCode doctor",
        f"Status: {report.overall_status}",
        f"Probe: {'enabled' if report.probe_enabled else 'disabled'}",
    ]
    if report.active_model_ref:
        lines.append(f"Active model: {report.active_model_ref}")
    lines.append("")

    grouped: dict[str, list[DiagnosticItem]] = defaultdict(list)
    for item in report.items:
        grouped[item.category].append(item)

    # category 排序让输出稳定，便于测试和人工比较。
    for category in sorted(grouped):
        lines.append(f"{category}:")
        for item in grouped[category]:
            lines.append(f"  [{item.status}] {item.name}: {item.message}")
            for key, value in sorted(item.details.items()):
                rendered = _render_detail(value)
                if rendered:
                    # details 可能包含空值；_render_detail 返回空字符串时就不展示。
                    lines.append(f"      {key}: {rendered}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _check_config(config: RuntimeConfig, report: DoctorReport) -> None:
    """检查配置加载阶段收集到的 warning，以及配置根目录。"""
    if config.config_errors:
        for error in config.config_errors:
            report.add("WARN", "config", "configuration", error)
    else:
        report.add("OK", "config", "configuration", "configuration files parsed without warnings")
    report.add(
        "OK",
        "config",
        "config roots",
        f"{len(config.config_roots)} config root(s) considered",
        roots=[str(path) for path in config.config_roots],
    )


def _check_workspace(config: RuntimeConfig, report: DoctorReport) -> None:
    """检查 cwd、repo_root、workspace_roots 和 BigCode home 目录。"""
    report.add("OK", "workspace", "cwd", str(config.cwd))
    report.add("OK", "workspace", "repo root", str(config.repo_root))
    missing_roots = [str(path) for path in config.workspace_roots if not path.exists()]
    if missing_roots:
        report.add("ERROR", "workspace", "workspace roots", "one or more workspace roots do not exist", roots=missing_roots)
    else:
        report.add("OK", "workspace", "workspace roots", f"{len(config.workspace_roots)} workspace root(s)", roots=[str(path) for path in config.workspace_roots])
    report.add("OK", "workspace", "sandbox profile", config.sandbox_profile)
    home_status = "OK" if config.bigcode_home.exists() else "WARN"
    home_msg = str(config.bigcode_home) if config.bigcode_home.exists() else f"{config.bigcode_home} does not exist yet"
    report.add(home_status, "workspace", "bigcode home", home_msg)


def _check_models(
    config: RuntimeConfig,
    active_model_ref: str | None,
    report: DoctorReport,
    env: dict[str, str],
) -> ResolvedModel | None:
    """检查模型注册表、当前模型引用和鉴权配置。"""
    if not config.models:
        report.add("ERROR", "provider", "models registry", "no models are configured")
        return None
    report.add("OK", "provider", "models registry", f"{len(config.models)} model(s) configured")
    if not active_model_ref:
        report.add("ERROR", "provider", "active model", "no default model configured; set models.json default_model or pass --model")
        return None
    model = config.models.get(active_model_ref)
    if not model:
        report.add("ERROR", "provider", "active model", f"model {active_model_ref!r} is not present in models registry")
        return None

    report.add(
        "OK",
        "provider",
        "active profile",
        f"{model.provider_type} provider {model.provider!r} -> model id {model.model_id!r}",
        base_url=model.base_url,
        api_key_env=model.api_key_env or "(none)",
        capabilities=_capabilities(model),
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
    )
    if model.api_key_env:
        # 如果配置了 api_key_env，优先看环境变量；但某些 provider 也允许
        # 直接在 default_headers 里配置 Authorization，所以这里也兼容。
        if env.get(model.api_key_env):
            report.add("OK", "provider", "api key", f"environment variable {model.api_key_env} is set")
        elif _has_auth_header(model.default_headers):
            report.add("OK", "provider", "api key", "auth header is provided by provider default_headers", header="configured")
        else:
            report.add("ERROR", "provider", "api key", f"missing environment variable {model.api_key_env}")
    elif _has_auth_header(model.default_headers):
        report.add("OK", "provider", "api key", "auth header is provided by provider default_headers", header="configured")
    else:
        report.add("WARN", "provider", "api key", "no api_key_env or auth header configured; provider must allow unauthenticated requests")
    return model


def _check_tools(report: DoctorReport, registry: ToolRegistry | None) -> None:
    """检查工具注册表能否构建，并统计各权限分类的工具数量。"""
    try:
        registry = registry or build_default_registry()
        tools = registry.list_tools()
    except Exception as exc:
        report.add("ERROR", "tools", "registry", f"failed to build default registry: {_format_exception(exc)}")
        return
    categories: dict[str, int] = defaultdict(int)
    for tool in tools:
        categories[str(tool.permission_category)] += 1
    report.add(
        "OK",
        "tools",
        "registry",
        f"{len(tools)} tool(s) registered",
        categories=dict(sorted(categories.items())),
        tools=[tool.name for tool in tools],
    )


def _check_skills(config: RuntimeConfig, report: DoctorReport, skill_registry: SkillRegistry | None) -> None:
    """检查技能根目录和技能加载报告。"""
    registry = skill_registry or load_skills(config.skill_roots)
    existing_roots = [str(path) for path in config.skill_roots if path.exists()]
    missing_roots = [str(path) for path in config.skill_roots if not path.exists()]
    report.add("OK", "skills", "roots", f"{len(existing_roots)} existing root(s), {len(missing_roots)} missing root(s)", existing=existing_roots, missing=missing_roots)
    counts = registry.status_counts()
    status: DiagnosticStatus = "WARN" if counts["failed"] else "OK"
    report.add(
        status,
        "skills",
        "registry",
        f"{counts['enabled']} enabled, {counts['disabled']} disabled, {counts['failed']} failed",
        skills=[skill.name for skill in registry.list()],
        counts=counts,
    )
    for item in registry.reports:
        # registry.reports 既包含 enabled，也包含 disabled/failed。
        # doctor 只展开 disabled/failed，让报告更聚焦问题。
        if item.status == "disabled":
            report.add("WARN", "skills", item.name, item.reason or "skill disabled", source=item.source, path=item.path, plugin=item.plugin_name or "")
        elif item.status == "failed":
            report.add("WARN", "skills", item.name, item.reason or "skill failed to load", source=item.source, path=item.path, plugin=item.plugin_name or "")


def _check_commands(config: RuntimeConfig, report: DoctorReport) -> None:
    """检查命令 hook 配置是否能被解析和启用。"""
    registry = CommandRegistry.from_settings(config.hooks, plugin_roots=config.skill_roots)
    counts = registry.status_counts()
    status: DiagnosticStatus = "WARN" if counts["failed"] or counts["disabled"] else "OK"
    report.add(
        status,
        "commands",
        "registry",
        f"{counts['enabled']} enabled, {counts['disabled']} disabled, {counts['failed']} failed",
        counts=counts,
        commands=[item.command for item in registry.registrations if item.status == "enabled"],
    )
    for item in registry.registrations:
        if item.status == "enabled":
            continue
        # disabled 也作为 WARN 展示，因为它可能代表插件声明了当前版本还不支持的 command。
        report.add(
            "WARN",
            "commands",
            item.id,
            item.reason or item.status,
            event=item.event,
            matcher=item.matcher or "",
            command=item.command,
            source=item.source,
        )


async def _check_mcp(
    config: RuntimeConfig,
    report: DoctorReport,
    *,
    probe: bool,
    mcp_manager: McpClientManager | None,
) -> None:
    """检查 MCP 是否启用、FastMCP 是否可用，并可选做 discovery probe。"""
    manager = mcp_manager or McpClientManager(config.mcp_servers, enabled=config.mcp_enabled)
    if not config.mcp_enabled:
        report.add("OK", "mcp", "status", "MCP is disabled")
        return
    enabled_servers = [server for server in config.mcp_servers.values() if server.enabled]
    disabled_servers = [server.name for server in config.mcp_servers.values() if not server.enabled]
    report.add("OK", "mcp", "configuration", f"{len(enabled_servers)} enabled server(s), {len(disabled_servers)} disabled server(s)", disabled=disabled_servers)
    if not enabled_servers:
        return
    if not manager.fastmcp_available:
        report.add("WARN", "mcp", "fastmcp", "FastMCP is not installed; MCP tools will report dependency errors")
        return
    report.add("OK", "mcp", "fastmcp", "FastMCP is importable")
    if not probe:
        # --no-probe 时不连接外部 MCP server，只报告本地配置状态。
        report.add("WARN", "mcp", "probe", "MCP discovery probe disabled")
        return
    for server in enabled_servers:
        try:
            capabilities = await manager.discover(server.name)
        except Exception as exc:
            report.add("ERROR", "mcp", server.name, f"discovery failed: {_format_exception(exc)}")
            continue
        status: DiagnosticStatus = "OK" if capabilities else "WARN"
        report.add(status, "mcp", server.name, f"{len(capabilities)} capability item(s) discovered")


async def _check_provider_probe(
    model: ResolvedModel | None,
    report: DoctorReport,
    *,
    probe: bool,
    timeout: float,
    env: dict[str, str],
) -> None:
    """可选向 provider 发起一次最小请求，验证模型端点真的可用。"""
    if not model:
        report.add("WARN", "provider", "probe", "provider probe skipped because active model is not resolved")
        return
    if not probe:
        report.add("WARN", "provider", "probe", "provider probe disabled")
        return
    if model.api_key_env and not env.get(model.api_key_env) and not _has_auth_header(model.default_headers):
        # 缺密钥时直接跳过网络请求，避免得到一个不如本地诊断清晰的 401。
        report.add("WARN", "provider", "probe", "provider probe skipped because API key is missing")
        return
    try:
        await probe_provider(model, timeout=timeout, env=env)
    except Exception as exc:
        report.add("ERROR", "provider", "probe", f"provider request failed: {_format_exception(exc)}")
        return
    report.add("OK", "provider", "probe", "provider accepted a minimal Claude-compatible messages request")


async def probe_provider(model: ResolvedModel, *, timeout: float = 10.0, env: dict[str, str] | None = None) -> dict[str, Any]:
    """向模型 provider 发一个最小 messages 请求，验证 base_url、鉴权和协议是否可用。"""
    env = env or os.environ
    api_key = env.get(model.api_key_env or "")
    headers = {"anthropic-version": "2023-06-01"}
    if api_key:
        headers["x-api-key"] = api_key
    headers.update(model.default_headers)
    if model.api_key_env and not api_key and not _has_auth_header(headers):
        raise RuntimeError(f"missing API key environment variable {model.api_key_env}")
    url = f"{model.base_url.rstrip('/')}/messages"

    # max_tokens=1 的 ping 请求成本低，但能验证 URL、模型 id、鉴权和 Claude Messages 协议。
    payload = {
        "model": model.model_id,
        "system": "BigCode doctor probe.",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "ping"}]}],
        "max_tokens": 1,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("provider response was not valid JSON") from exc
    return data if isinstance(data, dict) else {"response": data}


def _capabilities(model: ResolvedModel) -> list[str]:
    """把模型能力布尔值转换成报告里展示的短标签。"""
    caps = []
    if model.capabilities.supports_images:
        caps.append("images")
    if model.capabilities.supports_tools:
        caps.append("tools")
    if model.capabilities.supports_parallel_tool_calls:
        caps.append("parallel_tools")
    if model.capabilities.supports_thinking:
        caps.append("thinking")
    return caps or ["text"]


def _has_auth_header(headers: dict[str, str]) -> bool:
    """检查 headers 中是否已经包含常见鉴权字段。"""
    lowered = {key.lower() for key in headers}
    return bool(lowered.intersection({"x-api-key", "authorization", "api-key"}))


def _format_exception(exc: Exception) -> str:
    """把异常格式化成非空字符串。"""
    return str(exc).strip() or exc.__class__.__name__


def _render_detail(value: Any) -> str:
    """把诊断详情里的任意值渲染成一行短文本。"""
    if value is None:
        return ""
    if isinstance(value, dict):
        if not value:
            return "{}"
        return ", ".join(f"{key}={val}" for key, val in value.items())
    if isinstance(value, (list, tuple, set)):
        values = [str(item) for item in value]
        if len(values) > 12:
            values = [*values[:12], f"... +{len(value) - 12} more"]
        return ", ".join(values) if values else "[]"
    return str(value)
