"""所有工具共享的基础类型和抽象基类。

学习思路：新增工具通常继承 BaseTool，声明 input_model、权限分类和 call() 实现。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, Awaitable, Callable, Generic, Literal, TypeVar

from pydantic import BaseModel


InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT")

PermissionBehavior = Literal["allow", "deny", "ask", "passthrough"]
PermissionDecisionReasonType = Literal["ordinary", "rule", "safetyCheck", "requiresUserInteraction", "mode", "hook"]
PermissionCategory = Literal["read", "write", "edit", "delete", "bash", "network", "agent", "skill", "mcp", "state"]
StateEffect = Literal["none", "read_file_state", "workspace_write", "app_state", "external"]


@dataclass
class ValidationResult:
    """结构化结果数据。

    这种 dataclass 主要用于在模块之间传递信息，比直接使用 dict 更容易看出字段含义。
    """
    ok: bool
    message: str = ""
    error_code: int = 0


@dataclass
class PermissionDecision:
    """权限检查的返回值。

    behavior 决定 allow/deny/ask/passthrough，message/reason 用来解释给用户或日志。
    """
    behavior: PermissionBehavior
    message: str = ""
    updated_input: BaseModel | None = None
    reason: str = ""
    rule: str | None = None
    decision_reason: dict[str, Any] = field(default_factory=dict)

    @property
    def reason_type(self) -> str:
        """返回权限决定的原因类别；缺省是普通决策。"""
        return str(self.decision_reason.get("type") or "ordinary")


@dataclass
class ToolResult(Generic[OutputT]):
    """结构化结果数据。

    这种 dataclass 主要用于在模块之间传递信息，比直接使用 dict 更容易看出字段含义。
    """
    data: OutputT
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolRunResult(Generic[OutputT]):
    """结构化结果数据。

    这种 dataclass 主要用于在模块之间传递信息，比直接使用 dict 更容易看出字段含义。
    """
    tool_use_id: str
    tool_name: str
    output: ToolResult[OutputT] | None = None
    is_error: bool = False
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolExecutionContext:
    """工具运行时上下文。

    所有工具都通过它访问 cwd、workspace_roots、权限、hook、任务、技能、MCP 和会话状态。
    """
    cwd: Path
    workspace_roots: list[Path]
    permission_context: "ToolPermissionContext"
    read_file_state: "ReadFileState"
    abort_event: Event
    session_id: str
    hook_bus: "HookBus | None" = None
    is_non_interactive_session: bool = False
    plan_state: "PlanModeState | None" = None
    task_store: Any | None = None
    plan_store: Any | None = None
    skill_registry: Any | None = None
    mcp_manager: Any | None = None
    agent_session: Any | None = None
    artifact_store: Any | None = None
    project_state_dir: Path | None = None
    tool_registry: Any | None = None
    approval_callback: Callable[[str], Awaitable[bool]] | None = None
    terminal_interaction_callback: Callable[[Callable[[], Any]], Awaitable[Any]] | None = None
    approval_cache: dict[str, bool] | None = None
    force_turn_end: bool = False


class BaseTool(ABC, Generic[InputT, OutputT]):
    """一个可被模型调用的工具类。

    ToolRunner 会先校验 input_model 和权限，再调用这个类的 call() 方法执行真正逻辑。
    """
    name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    input_model: type[InputT]
    permission_category: PermissionCategory
    state_effect: StateEffect = "external"
    max_result_chars: int = 100_000
    should_defer: bool = False
    always_load: bool = False
    is_mcp: bool = False
    search_hint: str = ""

    def is_enabled(self, ctx: ToolExecutionContext) -> bool:
        """判断工具在当前上下文是否可用；默认所有工具都启用。"""
        return True

    def is_concurrency_safe(self, input: InputT, ctx: ToolExecutionContext) -> bool:
        """判断工具是否可并发执行；默认跟随只读判断。"""
        return self.is_read_only(input, ctx)

    def is_read_only(self, input: InputT, ctx: ToolExecutionContext) -> bool:
        """判断当前输入是否只读取外部或内部状态，不会产生持久副作用。"""
        return self.state_effect == "none"

    async def validate_input(self, input: InputT, ctx: ToolExecutionContext) -> ValidationResult:
        """执行工具前的业务校验。

        返回 ValidationResult(False, message) 会阻止工具继续运行。
        """
        return ValidationResult(ok=True)

    async def check_permissions(self, input: InputT, ctx: ToolExecutionContext) -> PermissionDecision:
        """工具自己的权限补充判断。

        返回 passthrough 表示交给通用 permissions.py 继续决策。
        """
        return PermissionDecision(behavior="passthrough", updated_input=input)

    def json_schema(self) -> dict[str, Any]:
        """返回 input_model 的 JSON Schema，供模型了解工具参数格式。"""
        return self.input_model.model_json_schema()

    @abstractmethod
    async def call(
        self,
        input: InputT,
        ctx: ToolExecutionContext,
        on_progress: Any | None = None,
    ) -> ToolResult[OutputT]:
        """工具执行入口。

        input 是已经通过 Pydantic 校验的参数，ctx 提供 cwd、权限、会话状态等运行环境。
        """
        raise NotImplementedError


class EmptyInput(BaseModel):
    """工具输入模型。

    Pydantic 会根据这些字段校验模型传进来的参数，字段类型就是最重要的阅读线索。
    """
    pass
