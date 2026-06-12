"""把磁盘配置、环境变量和命令行覆盖项合并成 RuntimeConfig。

学习思路：这个文件是启动阶段的配置流水线，先找根目录和配置层，再解析模型、权限、MCP、技能、计划目录等。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from bigcode.tools.permissions import PermissionRule, ToolPermissionContext, parse_permission_rule_string
from bigcode.utils.ids import project_id_for_path
from bigcode.utils.jsonio import deep_merge, read_json_file

from .models import McpServerConfig, ModelCapabilities, ResolvedModel, RuntimeConfig


_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MODEL_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def find_repo_root(cwd: Path) -> Path:
    """从 cwd 向上找 .git 目录，找不到就把 cwd 当作项目根。"""
    cur = cwd.resolve(strict=True)
    for path in [cur, *cur.parents]:
        if (path / ".git").exists():
            return path
    return cur


def load_runtime_config(
    cwd: Path | str,
    *,
    cli_overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> RuntimeConfig:
    """加载运行配置的主入口。

    配置来源按 home、repo_root/.bigcode、cwd/.bigcode 叠加，再叠加命令行覆盖和环境变量。
    """
    env = env or os.environ
    cwd_path = Path(cwd).resolve(strict=True)
    repo_root = find_repo_root(cwd_path)
    home = Path(env.get("BIGCODE_HOME", str(Path.home() / ".bigcode"))).expanduser()
    project_state_dir = home / "projects" / project_id_for_path(repo_root)

    # 配置是分层加载的：用户级 home 最先读，项目级和当前目录级后读。
    # 后面的层会覆盖前面的同名字段，因此 cwd/.bigcode 可以覆盖 repo/.bigcode。
    roots = _dedupe_existing(
        [
            home,
            repo_root / ".bigcode",
            cwd_path / ".bigcode",
        ]
    )
    errors: list[str] = []

    settings = _load_layered_json(roots, "settings.json", errors)
    models_json = _load_layered_json(roots, "models.json", errors)
    mcp_json = _load_layered_json(roots, "mcp.json", errors)
    cli_overrides = cli_overrides or {}

    if cli_overrides:
        # 命令行参数优先级最高，只覆盖 settings 这一层运行设置。
        settings = deep_merge(settings, cli_overrides)

    # 解析阶段不直接抛出大多数配置错误，而是收集到 errors。
    # 这样 /doctor 和启动提示可以一次性展示多个问题。
    permission_context = _parse_permissions(settings.get("permissions") or {}, errors)
    workspace_roots = _resolve_workspace_roots(cwd_path, settings.get("workspace_roots") or [], errors)
    sandbox_profile = _parse_sandbox_profile(settings.get("sandbox") or {}, errors)

    models, model_errors = _parse_models(models_json)
    errors.extend(model_errors)
    default_model_ref = (
        cli_overrides.get("default_model")
        or env.get("BIGCODE_MODEL")
        or settings.get("default_model")
        or models_json.get("default_model")
    )
    if default_model_ref and default_model_ref not in models:
        errors.append(f"default model {default_model_ref!r} is not present in models registry")

    # MCP 的 enabled 开关放在 bigcode.mcp 下；具体 server 列表仍按 mcpServers 解析。
    mcp_settings = deep_merge(settings.get("bigcode", {}).get("mcp", {}) or {}, mcp_json.get("bigcode", {}).get("mcp", {}) or {})
    mcp_enabled = bool(mcp_settings.get("enabled", True))
    mcp_servers = _parse_mcp_servers(mcp_json.get("mcpServers") or {}, errors)

    # plan.default_dir 可以写相对路径；相对路径以 cwd 为基准，方便不同项目各自保存计划。
    plan_dir = Path(settings.get("plan", {}).get("default_dir", ".bigcode/plans"))
    if not plan_dir.is_absolute():
        plan_dir = cwd_path / plan_dir
    plan_dir = plan_dir.resolve(strict=False)

    # 技能和 agent 的根目录会保留不存在路径。扫描时会跳过不存在的目录，
    # 但 /doctor 可以把这些路径展示出来，帮助排查配置。
    skill_roots = _dedupe_paths(
        [
            home / "skills",
            Path("/home/qt/.agents/skills"),
            repo_root / ".bigcode" / "skills",
            cwd_path / ".bigcode" / "skills",
        ]
    )
    agent_roots = _dedupe_paths(
        [
            home / "agents",
            repo_root / ".bigcode" / "agents",
            cwd_path / ".bigcode" / "agents",
        ]
    )
    instruction_paths = _instruction_paths(home, repo_root, cwd_path)

    task_default = env.get("BIGCODE_TASK_LIST_ID") or settings.get("tasks", {}).get("default_task_list_id")

    return RuntimeConfig(
        cwd=cwd_path,
        repo_root=repo_root,
        bigcode_home=home,
        project_state_dir=project_state_dir,
        config_roots=roots,
        default_model_ref=default_model_ref,
        models=models,
        workspace_roots=workspace_roots,
        permission_context=permission_context,
        hooks=settings.get("hooks") or {},
        mcp_servers=mcp_servers,
        mcp_enabled=mcp_enabled,
        skill_roots=skill_roots,
        agent_roots=agent_roots,
        instruction_paths=instruction_paths,
        plan_default_dir=plan_dir,
        task_default_list_id=task_default,
        sandbox_profile=sandbox_profile,
        config_errors=errors,
    )


def _load_layered_json(roots: list[Path], filename: str, errors: list[str]) -> dict[str, Any]:
    """按配置根目录顺序读取同名 JSON，并用 deep_merge 合并。"""
    merged: dict[str, Any] = {}
    for root in roots:
        data, error = read_json_file(root / filename)
        if error:
            errors.append(error)
        if data:
            merged = deep_merge(merged, data)
    return merged


def _dedupe_existing(paths: list[Path]) -> list[Path]:
    """解析路径并去重；名字保留了 existing，但当前实现不会要求路径真实存在。"""
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        try:
            resolved = path.expanduser().resolve(strict=False)
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    """对路径列表做 expanduser/resolve 并保持原顺序去重。"""
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


def _resolve_workspace_roots(cwd: Path, configured: list[Any], errors: list[str]) -> list[Path]:
    """解析用户配置的 workspace_roots，失败项会写入 errors。"""
    roots = [cwd]
    for item in configured:
        try:
            roots.append(Path(str(item)).expanduser().resolve(strict=True))
        except Exception as exc:
            errors.append(f"workspace root {item!r} is invalid: {exc}")
    return _dedupe_paths(roots)


def _parse_permissions(data: dict[str, Any], errors: list[str]) -> ToolPermissionContext:
    """把 settings.json 中的 permissions 字段转为 ToolPermissionContext。"""
    mode = data.get("mode", "default")
    if mode not in {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"}:
        # 配置错误不直接中断启动，而是降级到 default 并记录 warning。
        # 这样用户还能运行 /doctor 看到完整诊断。
        errors.append(f"invalid permission mode {mode!r}; using default")
        mode = "default"

    def rules(key: str) -> list[PermissionRule]:
        """解析 allow/deny/ask 或 always_* 中的一组规则。

        字符串 Bash 是整工具规则，Bash(pattern) 是内容级规则。
        字典可以额外指定 tool/tool_name、pattern、behavior 等字段。
        """
        parsed: list[PermissionRule] = []
        for raw in data.get(key) or []:
            behavior = _behavior_for_key(key)
            if isinstance(raw, str):
                parsed.append(parse_permission_rule_string(raw, behavior, source="config"))
            elif isinstance(raw, dict):
                tool_name = str(raw.get("tool_name") or raw.get("tool") or "*")
                parsed.append(
                    PermissionRule(
                        tool_name=tool_name,
                        behavior=raw.get("behavior") or behavior,
                        pattern=raw.get("pattern"),
                        source="config",
                        reason=str(raw.get("reason") or ""),
                    )
                )
        return parsed

    return ToolPermissionContext(
        mode=mode,
        always_allow=[*rules("allow"), *rules("always_allow")],
        always_deny=[*rules("deny"), *rules("always_deny")],
        always_ask=[*rules("ask"), *rules("always_ask")],
        should_avoid_permission_prompts=bool(data.get("should_avoid_permission_prompts", False)),
    )


def _parse_sandbox_profile(data: dict[str, Any], errors: list[str]) -> str:
    """解析 sandbox.profile，非法值降级到 none。"""
    profile = str(data.get("profile") or "none")
    if profile not in {"none", "read-only", "workspace"}:
        errors.append(f"invalid sandbox profile {profile!r}; using none")
        return "none"
    return profile


def _behavior_for_key(key: str) -> str:
    """根据配置键名 always_allow/always_deny/always_ask 推断行为。"""
    if key.endswith("allow"):
        return "allow"
    if key.endswith("deny"):
        return "deny"
    return "ask"


def _parse_models(data: dict[str, Any]) -> tuple[dict[str, ResolvedModel], list[str]]:
    """解析 models.json 的 providers/models，生成 provider:model_key 形式的 ResolvedModel 表。"""
    errors: list[str] = []
    out: dict[str, ResolvedModel] = {}
    providers = data.get("providers") or {}
    if not isinstance(providers, dict):
        return out, ["models.json providers must be an object"]
    for provider_name, provider in providers.items():
        if not _NAME_RE.match(str(provider_name)):
            errors.append(f"invalid provider name {provider_name!r}")
            continue
        if not isinstance(provider, dict):
            errors.append(f"provider {provider_name!r} must be an object")
            continue

        # provider 是服务商级配置：base_url、鉴权 header、api_key_env 等。
        # models 是该服务商下面的具体模型列表。
        provider_type = provider.get("type", "claude-compatible")
        if provider_type == "openai-compatible":
            errors.append(f"provider {provider_name!r}: type 'openai-compatible' is deprecated; treating as 'claude-compatible'")
            provider_type = "claude-compatible"
        if provider_type != "claude-compatible":
            errors.append(f"provider {provider_name!r}: unsupported type {provider_type!r}")
            continue
        base_url = provider.get("base_url")
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            errors.append(f"provider {provider_name!r}: invalid base_url")
            continue
        models = provider.get("models") or {}
        if not isinstance(models, dict):
            errors.append(f"provider {provider_name!r}: models must be an object")
            continue
        for model_key, model in models.items():
            # model_key 是 BigCode 配置里的短名。最终模型引用会变成
            # provider:model_key，例如 "anthropic:sonnet"。
            if not _MODEL_KEY_RE.match(str(model_key)):
                errors.append(f"provider {provider_name!r}: invalid model key {model_key!r}")
                continue
            if not isinstance(model, dict):
                errors.append(f"provider {provider_name!r}: model {model_key!r} must be an object")
                continue
            api_key_env = _parse_api_key_env(provider_name=str(provider_name), raw=provider.get("api_key_env"), errors=errors)
            ref = f"{provider_name}:{model_key}"
            caps = model.get("capabilities") or {}

            # ResolvedModel 是“已经拍平”的最终配置，后续请求模型时不再回头查原始 JSON。
            out[ref] = ResolvedModel(
                ref=ref,
                provider=str(provider_name),
                model_key=str(model_key),
                model_id=str(model.get("id") or model_key),
                base_url=base_url.rstrip("/"),
                api_key_env=api_key_env,
                default_headers=dict(provider.get("default_headers") or {}),
                capabilities=ModelCapabilities(
                    supports_images=bool(caps.get("supports_images", False)),
                    supports_tools=bool(caps.get("supports_tools", True)),
                    supports_parallel_tool_calls=bool(caps.get("supports_parallel_tool_calls", False)),
                    supports_thinking=bool(caps.get("supports_thinking", False)),
                ),
                context_window=model.get("context_window"),
                max_output_tokens=model.get("max_output_tokens"),
                provider_type=provider_type,
            )
    return out, errors


def _parse_api_key_env(*, provider_name: str, raw: Any, errors: list[str]) -> str | None:
    """校验 api_key_env 是否像环境变量名，而不是把密钥明文写进配置。"""
    if raw is None:
        return None
    if not isinstance(raw, str):
        errors.append(f"provider {provider_name!r}: api_key_env must be an environment variable name")
        return None
    if not _ENV_VAR_RE.match(raw):
        if _looks_like_secret(raw):
            errors.append(
                f"provider {provider_name!r}: api_key_env looks like a plaintext token; "
                "store the token in the environment and put only the variable name in models.json"
            )
        else:
            errors.append(f"provider {provider_name!r}: api_key_env must be an environment variable name")
        return None
    return raw


def _looks_like_secret(value: str) -> bool:
    """用简单启发式判断字符串是否像 API key。"""
    lowered = value.lower()
    if lowered.startswith(("sk-", "tp-", "ak-", "pk-")):
        return True
    return len(value) >= 24 and any(ch.isdigit() for ch in value) and any(ch.isalpha() for ch in value)


def _parse_mcp_servers(data: dict[str, Any], errors: list[str]) -> dict[str, McpServerConfig]:
    """解析 mcp.json 中的 mcpServers。"""
    out: dict[str, McpServerConfig] = {}
    if not isinstance(data, dict):
        errors.append("mcpServers must be an object")
        return out
    for name, cfg in data.items():
        if not _NAME_RE.match(str(name)):
            errors.append(f"invalid MCP server name {name!r}")
            continue
        if not isinstance(cfg, dict):
            errors.append(f"MCP server {name!r} must be an object")
            continue
        out[str(name)] = McpServerConfig(name=str(name), config=cfg, enabled=bool(cfg.get("enabled", True)))
    return out


def _instruction_paths(home: Path, repo_root: Path, cwd: Path) -> list[Path]:
    """按优先级生成可能存在的项目说明文件路径列表。"""
    paths = [
        home / "instructions.md",
        repo_root / "BIGCODE.md",
        repo_root / ".bigcode" / "instructions.md",
    ]
    paths.extend(sorted((repo_root / ".bigcode" / "rules").glob("*.md")) if (repo_root / ".bigcode" / "rules").exists() else [])
    paths.extend(
        [
            cwd / "BIGCODE.md",
            cwd / ".bigcode" / "instructions.md",
        ]
    )
    paths.extend(sorted((cwd / ".bigcode" / "rules").glob("*.md")) if (cwd / ".bigcode" / "rules").exists() else [])
    paths.append(cwd / "BIGCODE.local.md")
    return _dedupe_paths(paths)
