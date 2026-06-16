"""配置层使用的数据模型。

学习思路：这里的 dataclass 不执行业务逻辑，只把“运行时需要的配置”集中成结构化对象，方便其它模块传递。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


ModelProtocol = Literal["anthropic", "openai"]


@dataclass(frozen=True)
class ModelCapabilities:
    """模型能力开关。

    不同 provider/model 支持的图片、工具、并行工具或 thinking 能力会在这里统一描述。
    """
    supports_images: bool = False
    supports_tools: bool = True
    supports_parallel_tool_calls: bool = False
    supports_thinking: bool = False


@dataclass(frozen=True)
class ResolvedModel:
    """已经解析完成的模型配置。

    它把 provider 配置和具体模型配置合成一次请求模型所需的全部信息。
    """
    ref: str
    provider: str
    model_key: str
    model_id: str
    base_url: str
    api_key_env: str | None
    protocol: ModelProtocol = "anthropic"
    default_headers: dict[str, str] = field(default_factory=dict)
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    context_window: int | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    thinking: bool = False
    provider_type: str = "claude-compatible"


@dataclass(frozen=True)
class McpServerConfig:
    """单个 MCP server 的配置包装。

    name 是配置表里的键，config 是原始 server 配置，enabled 控制是否启用。
    """
    name: str
    config: dict[str, Any]
    enabled: bool = True


@dataclass(frozen=True)
class CompactConfig:
    """上下文压缩阈值和保护参数。"""

    time_microcompact_enabled: bool = True
    time_microcompact_gap_minutes: int = 60
    time_microcompact_keep_recent: int = 5
    snip_enabled: bool = True
    snip_threshold: float = 0.70
    snip_target: float = 0.60
    snip_min_messages: int = 6
    snip_min_tokens: int = 2000
    context_collapse_enabled: bool = False
    collapse_threshold: float = 0.75
    collapse_target: float = 0.65
    collapse_min_tokens_saved: int = 2000
    collapse_max_spans_per_pass: int = 2
    auto_compact_enabled: bool = True
    auto_compact_threshold: float = 0.93
    auto_keep_tokens: int = 40000
    auto_min_keep_messages: int = 6
    auto_max_failures: int = 3
    blocked_threshold: float = 0.95
    protected_tail_messages: int = 8
    protected_tail_tokens: int = 8000


@dataclass(frozen=True)
class RuntimeConfig:
    """BigCode 启动后的总配置对象。

    后续 AgentSession 基本只拿这个对象，不再直接读取 settings/models/mcp 文件。
    """
    cwd: Path
    repo_root: Path
    bigcode_home: Path
    project_state_dir: Path
    config_roots: list[Path]
    default_model_ref: str | None
    models: dict[str, ResolvedModel]
    workspace_roots: list[Path]
    permission_context: "ToolPermissionContext"
    mcp_servers: dict[str, McpServerConfig]
    mcp_enabled: bool
    skill_roots: list[Path]
    agent_roots: list[Path]
    instruction_paths: list[Path]
    plan_default_dir: Path
    compact: CompactConfig = field(default_factory=CompactConfig)
    task_default_list_id: str | None = None
    sandbox_profile: str = "none"
    config_errors: list[str] = field(default_factory=list)
