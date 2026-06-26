"""配置层使用的数据模型。

学习思路：这里的 dataclass 不执行业务逻辑，只把"运行时需要的配置"集中成结构化对象，方便其它模块传递。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from bigcode.sandbox.models import SandboxConfig


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

    @property
    def description(self) -> str:
        return str(self.config.get("description", "")).strip()


@dataclass(frozen=True)
class CompactConfig:
    """上下文压缩阈值和保护参数。

    各层阈值 = ratio × B，其中 B = C - X - S - R 是运行时根据模型窗口动态计算的有效预算。
    这样不同窗口的模型自动获得合适的绝对阈值，层级关系始终成立。
    """

    # --- 各层触发 / 目标比例（作用于 B，不是 C）---
    snip_trigger_ratio: float = 0.65
    snip_target_ratio: float = 0.55
    micro_trigger_ratio: float = 0.75
    collapse_trigger_ratio: float = 0.85
    collapse_target_ratio: float = 0.70
    fast_trigger_ratio: float = 0.93
    # FULL = 1.0 × B，由传统大压缩使用

    # --- 摘要预留 X 的计算参数 ---
    summary_max_tokens: int = 20000
    summary_min_tokens: int = 4000
    summary_output_ratio: float = 0.10  # X ≤ C × 10%

    # --- 安全余量 S 的计算参数 ---
    safety_margin_min_tokens: int = 4000
    safety_margin_max_tokens: int = 20000
    safety_margin_ratio: float = 0.06  # S ≤ C × 6%

    # --- 工作预留 R ---
    working_reserve_tokens: int = 5000

    # --- 运行参数（未变）---
    time_microcompact_enabled: bool = True
    time_microcompact_gap_minutes: int = 60
    time_microcompact_keep_recent: int = 5
    snip_enabled: bool = True
    snip_min_messages: int = 6
    snip_min_tokens: int = 2000
    context_collapse_enabled: bool = False
    collapse_min_tokens_saved: int = 2000
    collapse_max_spans_per_pass: int = 2
    auto_compact_enabled: bool = True
    auto_keep_tokens: int = 40000
    auto_min_keep_messages: int = 6
    auto_max_failures: int = 3
    protected_tail_messages: int = 8
    protected_tail_tokens: int = 8000
    auto_keep_files: int = 5
    auto_keep_conversations: int = 3


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
    sandbox_config: "SandboxConfig | None" = None
    task_default_list_id: str | None = None
    config_errors: list[str] = field(default_factory=list)
