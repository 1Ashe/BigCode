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

from .models import CompactConfig, McpServerConfig, ModelCapabilities, ResolvedModel, RuntimeConfig


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
    compact_config = _parse_compact_config(settings.get("compact") or {}, errors)

    models, model_errors = _parse_models(models_json)
    errors.extend(model_errors)
    default_model_ref = (
        cli_overrides.get("default_model")
        or env.get("BIGCODE_MODEL")
        or settings.get("default_model")
        or models_json.get("default_model")
        or (models_json.get("defaults") or {}).get("model")
    )
    if default_model_ref and default_model_ref not in models:
        errors.append(f"default model {default_model_ref!r} is not present in models registry")
    elif default_model_ref and models[default_model_ref].context_window is None:
        errors.append(f"default model {default_model_ref!r} has no context_window; compact will use 128000")

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
        mcp_servers=mcp_servers,
        mcp_enabled=mcp_enabled,
        skill_roots=skill_roots,
        agent_roots=agent_roots,
        instruction_paths=instruction_paths,
        plan_default_dir=plan_dir,
        compact=compact_config,
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


def _parse_compact_config(data: dict[str, Any], errors: list[str]) -> CompactConfig:
    """解析 compact 设置；单项非法时回退该字段默认值。"""
    defaults = CompactConfig()
    if not isinstance(data, dict):
        errors.append("compact settings must be an object; using defaults")
        return defaults

    def boolean(name: str) -> bool:
        value = data.get(name, getattr(defaults, name))
        if isinstance(value, bool):
            return value
        errors.append(f"compact.{name} must be a boolean; using default")
        return getattr(defaults, name)

    def positive_int(name: str) -> int:
        value = data.get(name, getattr(defaults, name))
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        errors.append(f"compact.{name} must be a positive integer; using default")
        return getattr(defaults, name)

    def ratio(name: str) -> float:
        value = data.get(name, getattr(defaults, name))
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < float(value) < 1:
            return float(value)
        errors.append(f"compact.{name} must be between 0 and 1; using default")
        return getattr(defaults, name)

    config = CompactConfig(
        time_microcompact_enabled=boolean("time_microcompact_enabled"),
        time_microcompact_gap_minutes=positive_int("time_microcompact_gap_minutes"),
        time_microcompact_keep_recent=positive_int("time_microcompact_keep_recent"),
        snip_enabled=boolean("snip_enabled"),
        snip_threshold=ratio("snip_threshold"),
        snip_target=ratio("snip_target"),
        snip_min_messages=positive_int("snip_min_messages"),
        snip_min_tokens=positive_int("snip_min_tokens"),
        context_collapse_enabled=boolean("context_collapse_enabled"),
        collapse_threshold=ratio("collapse_threshold"),
        collapse_target=ratio("collapse_target"),
        collapse_min_tokens_saved=positive_int("collapse_min_tokens_saved"),
        collapse_max_spans_per_pass=positive_int("collapse_max_spans_per_pass"),
        auto_compact_enabled=boolean("auto_compact_enabled"),
        auto_compact_threshold=ratio("auto_compact_threshold"),
        auto_keep_tokens=positive_int("auto_keep_tokens"),
        auto_min_keep_messages=positive_int("auto_min_keep_messages"),
        auto_max_failures=positive_int("auto_max_failures"),
        blocked_threshold=ratio("blocked_threshold"),
        protected_tail_messages=positive_int("protected_tail_messages"),
        protected_tail_tokens=positive_int("protected_tail_tokens"),
    )
    if config.snip_target >= config.snip_threshold:
        errors.append("compact.snip_target must be lower than snip_threshold; using defaults")
        config = CompactConfig(**{**config.__dict__, "snip_target": defaults.snip_target, "snip_threshold": defaults.snip_threshold})
    if config.collapse_target >= config.collapse_threshold:
        errors.append("compact.collapse_target must be lower than collapse_threshold; using defaults")
        config = CompactConfig(
            **{
                **config.__dict__,
                "collapse_target": defaults.collapse_target,
                "collapse_threshold": defaults.collapse_threshold,
            }
        )
    highest_trigger = max(config.snip_threshold, config.collapse_threshold, config.auto_compact_threshold)
    if config.blocked_threshold <= highest_trigger:
        errors.append("compact.blocked_threshold must exceed all compact thresholds; using default thresholds")
        config = CompactConfig(
            **{
                **config.__dict__,
                "snip_threshold": defaults.snip_threshold,
                "collapse_threshold": defaults.collapse_threshold,
                "auto_compact_threshold": defaults.auto_compact_threshold,
                "blocked_threshold": defaults.blocked_threshold,
            }
        )
    return config


def _behavior_for_key(key: str) -> str:
    """根据配置键名 always_allow/always_deny/always_ask 推断行为。"""
    if key.endswith("allow"):
        return "allow"
    if key.endswith("deny"):
        return "deny"
    return "ask"


def _parse_models(data: dict[str, Any]) -> tuple[dict[str, ResolvedModel], list[str]]:
    """解析模型配置，兼容旧嵌套格式和新 provider/model 分离格式。"""
    errors: list[str] = []
    out: dict[str, ResolvedModel] = {}
    providers = data.get("providers") or {}
    if not isinstance(providers, dict):
        return out, ["models.json providers must be an object"]

    provider_infos: dict[str, dict[str, Any]] = {}
    for provider_name, provider in providers.items():
        info = _parse_provider_info(str(provider_name), provider, errors)
        if info is not None:
            provider_infos[str(provider_name)] = info

    # 旧格式：providers.<name>.models.<key>。
    for provider_name, info in provider_infos.items():
        models = info["raw"].get("models") or {}
        if not models:
            continue
        if not isinstance(models, dict):
            errors.append(f"provider {provider_name!r}: models must be an object")
            continue
        for model_key, model in models.items():
            if not _MODEL_KEY_RE.match(str(model_key)):
                errors.append(f"provider {provider_name!r}: invalid model key {model_key!r}")
                continue
            if not isinstance(model, dict):
                errors.append(f"provider {provider_name!r}: model {model_key!r} must be an object")
                continue
            ref = f"{provider_name}:{model_key}"
            out[ref] = _resolved_model_from_parts(ref, provider_name, str(model_key), model, info)

    # 新格式：models.<ref>.provider 指向 providers.<name>。
    top_models = data.get("models") or {}
    if top_models:
        if not isinstance(top_models, dict):
            errors.append("models.json models must be an object")
        else:
            for model_ref, model in top_models.items():
                if not _MODEL_KEY_RE.match(str(model_ref)):
                    errors.append(f"invalid model ref {model_ref!r}")
                    continue
                if not isinstance(model, dict):
                    errors.append(f"model {model_ref!r} must be an object")
                    continue
                provider_name = str(model.get("provider") or "")
                if not provider_name:
                    errors.append(f"model {model_ref!r}: provider is required")
                    continue
                info = provider_infos.get(provider_name)
                if info is None:
                    errors.append(f"model {model_ref!r}: provider {provider_name!r} is not configured")
                    continue
                out[str(model_ref)] = _resolved_model_from_parts(str(model_ref), provider_name, str(model_ref), model, info)
    return out, errors


def _parse_provider_info(provider_name: str, provider: Any, errors: list[str]) -> dict[str, Any] | None:
    """解析 provider 级配置。"""
    if not _NAME_RE.match(provider_name):
        errors.append(f"invalid provider name {provider_name!r}")
        return None
    if not isinstance(provider, dict):
        errors.append(f"provider {provider_name!r} must be an object")
        return None

    protocol = provider.get("protocol")
    provider_type = provider.get("type", "claude-compatible")
    if protocol is None:
        if provider_type == "openai-compatible":
            errors.append(f"provider {provider_name!r}: type 'openai-compatible' is deprecated; treating as anthropic protocol")
            provider_type = "claude-compatible"
        if provider_type != "claude-compatible":
            errors.append(f"provider {provider_name!r}: unsupported type {provider_type!r}")
            return None
        protocol = "anthropic"
    if protocol not in {"anthropic", "openai"}:
        errors.append(f"provider {provider_name!r}: unsupported protocol {protocol!r}")
        return None

    base_url = provider.get("base_url")
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        errors.append(f"provider {provider_name!r}: invalid base_url")
        return None

    headers = dict(provider.get("default_headers") or {})
    anthropic_version = provider.get("anthropic_version")
    if anthropic_version and "anthropic-version" not in {key.lower() for key in headers}:
        headers["anthropic-version"] = str(anthropic_version)

    return {
        "raw": provider,
        "protocol": str(protocol),
        "provider_type": str(provider_type if protocol == "anthropic" else protocol),
        "base_url": base_url.rstrip("/"),
        "api_key_env": _parse_api_key_env(provider_name=provider_name, raw=provider.get("api_key_env"), errors=errors),
        "default_headers": headers,
    }


def _resolved_model_from_parts(
    ref: str,
    provider_name: str,
    model_key: str,
    model: dict[str, Any],
    provider_info: dict[str, Any],
) -> ResolvedModel:
    """把 provider 和 model 两层配置拍平成运行时模型配置。"""
    caps = model.get("capabilities") or {}
    model_id = str(model.get("id") or model.get("model") or model_key)
    max_output_tokens = model.get("max_output_tokens", model.get("max_tokens"))
    return ResolvedModel(
        ref=ref,
        provider=provider_name,
        model_key=model_key,
        model_id=model_id,
        base_url=provider_info["base_url"],
        api_key_env=provider_info["api_key_env"],
        protocol=provider_info["protocol"],
        default_headers=dict(provider_info["default_headers"]),
        capabilities=ModelCapabilities(
            supports_images=bool(caps.get("supports_images", False)),
            supports_tools=bool(caps.get("supports_tools", True)),
            supports_parallel_tool_calls=bool(caps.get("supports_parallel_tool_calls", False)),
            supports_thinking=bool(caps.get("supports_thinking", False)),
        ),
        context_window=model.get("context_window"),
        max_output_tokens=max_output_tokens if isinstance(max_output_tokens, int) else None,
        temperature=_float_or_none(model.get("temperature")),
        thinking=bool(model.get("thinking", False)),
        provider_type=provider_info["provider_type"],
    )


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


def _float_or_none(value: Any) -> float | None:
    """把配置里的数字转成 float，非法值按未配置处理。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


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
