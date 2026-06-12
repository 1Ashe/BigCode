# Tool 系统设计：我的 Python 实现方案

## 1. 目标和边界

这个文档描述一个 Python Agent Tool 系统的实现方案。系统目标是让模型可以通过统一协议调用工具，完成文件读取、搜索、编辑、命令执行、网络获取、MCP 外部能力、子任务拆分、计划管理、用户提问和 Skill 调用，同时在权限、路径、并发和文件一致性上保持可审计、可测试、默认安全。

核心边界如下：

- `cwd` 是主工作区。
- `workspace_roots` 是额外授权目录，必须在启动时规范化为真实路径。
- 所有文件路径在执行前必须先完成规范化和权限判断。
- 所有写操作、危险命令、网络访问、MCP 调用、子 Agent 和 Skill 调用必须经过统一权限引擎。
- 工具执行前后必须经过 Hooks 系统的 `PreToolUse` / `PostToolUse` 生命周期点；hooks 只能收紧、补充上下文或改写待校验输入，不能绕过 hard deny、路径安全和工具约束。
- 工具执行侧必须限制原始输出大小，避免 stdout、网页正文或文件读取撑爆内存；进入模型上下文前的 `tool_result` 预算、落盘引用和 attachment 由 Context 系统负责。
- 子 Agent 不能提升父级权限，不能扩大 workspace 范围。
- 文件读取和编辑必须依赖 `ReadFileState`，既避免重复读取撑爆上下文，也避免覆盖用户或并发任务的修改。
- 默认策略是 fail closed：未知工具、未知权限类别、未知路径状态、复杂 shell 语法和无法归类的网络目标都不能静默放行。

不在当前设计范围内的内容不作为权限绕过理由。未来新增能力必须先声明权限类别、路径影响、状态影响、并发属性和验收测试，再接入工具注册表。

## 2. 推荐目录结构

```txt
bigcode/
  tools/
    base.py
    registry.py
    runner.py
    permissions.py
    paths.py
    read_file_state.py
    output_limits.py
    bash_tool.py
    read_tool.py
    edit_tool.py
    write_tool.py
    glob_tool.py
    grep_tool.py
    web_fetch_tool.py
    web_search_tool.py
    agent_tool.py
    todo_tool.py
    task_tool.py
    plan_mode_tool.py
    ask_user_question_tool.py
    skill_tool.py
    external_resource_tool.py
    external_prompt_tool.py
```

模块职责：

- `base.py`：定义 `BaseTool`、`ToolExecutionContext`、`ToolResult`、权限决策和状态影响类型。
- `registry.py`：注册工具、处理别名、拒绝未知工具。
- `runner.py`：执行模型返回的 `tool_use`，负责 schema 校验、`PreToolUse` / `PostToolUse` hooks、权限判断、调度、取消、错误映射和执行侧输出限制。
- `permissions.py`：统一权限引擎，处理模式、规则、硬拒绝、非交互归一化和工具约束。
- `paths.py`：路径规范化、workspace 判断、symlink 防穿透、文件类别识别。
- `read_file_state.py`：记录读取快照，为 `Read` 提供重复读去重，为 `Edit` / `Write` 提供一致性检查和文件级锁。
- `output_limits.py`：限制工具执行阶段的原始输出大小，防止进程内存和日志无限增长。

对应 Claude Code 源码参考：

- `/home/qt/claude-code-rev/src/tools.ts`：基础工具注册表和可用工具集合。
- `/home/qt/claude-code-rev/src/Tool.ts`：Tool 协议、权限上下文、执行上下文字段。
- `/home/qt/claude-code-rev/src/services/tools/toolExecution.ts`：模型 tool_use 到工具执行的调度入口。
- `/home/qt/claude-code-rev/src/utils/permissions/`：权限模式、规则匹配、Bash 分类、路径权限和拒绝策略。
- `/home/qt/claude-code-rev/src/tools/BashTool/`：Bash 工具、命令语义、安全分类和权限处理。
- `/home/qt/claude-code-rev/src/tools/FileReadTool/`、`/home/qt/claude-code-rev/src/tools/FileEditTool/`、`/home/qt/claude-code-rev/src/tools/FileWriteTool/`：文件读写工具实现。
- `/home/qt/claude-code-rev/src/tools/AskUserQuestionTool/`：交互型提问工具。
- `/home/qt/claude-code-rev/src/tools/EnterPlanModeTool/`、`/home/qt/claude-code-rev/src/tools/ExitPlanModeTool/ExitPlanModeV2Tool.ts`：Plan Mode 进入和退出审批工具。

## 3. 工具协议

工具使用 `pydantic` 输入模型。每个工具必须声明权限类别、状态影响和并发属性，不能依靠基类默认放行。

```py
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, Optional, TypeVar

from pydantic import BaseModel


InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT")

PermissionBehavior = Literal["allow", "deny", "ask", "passthrough"]
PermissionCategory = Literal[
    "read",
    "write",
    "edit",
    "delete",
    "bash",
    "network",
    "agent",
    "skill",
    "mcp",
    "state",
]
StateEffect = Literal["none", "read_file_state", "workspace_write", "app_state", "external"]


@dataclass
class ValidationResult:
    ok: bool
    message: str = ""
    error_code: int = 0


@dataclass
class PermissionDecision:
    behavior: PermissionBehavior
    message: str = ""
    updated_input: Optional[BaseModel] = None
    reason: str = ""
    rule: Optional[str] = None


@dataclass
class ToolResult(Generic[OutputT]):
    data: OutputT
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolRunResult(Generic[OutputT]):
    tool_use_id: str
    tool_name: str
    output: ToolResult[OutputT] | None = None
    is_error: bool = False
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseTool(ABC, Generic[InputT, OutputT]):
    name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    input_model: type[InputT]
    permission_category: PermissionCategory
    state_effect: StateEffect = "external"
    max_result_chars: int = 100_000

    def is_enabled(self, ctx: "ToolExecutionContext") -> bool:
        return True

    def is_concurrency_safe(self, input: InputT, ctx: "ToolExecutionContext") -> bool:
        return self.state_effect == "none"

    async def validate_input(
        self,
        input: InputT,
        ctx: "ToolExecutionContext",
    ) -> ValidationResult:
        return ValidationResult(ok=True)

    async def check_permissions(
        self,
        input: InputT,
        ctx: "ToolExecutionContext",
    ) -> PermissionDecision:
        return PermissionDecision(behavior="passthrough", updated_input=input)

    @abstractmethod
    async def call(
        self,
        input: InputT,
        ctx: "ToolExecutionContext",
        on_progress: Optional[Any] = None,
    ) -> ToolResult[OutputT]:
        raise NotImplementedError
```

基类规则：

- `BaseTool.check_permissions()` 默认只返回 `passthrough`，表示交给统一权限引擎继续判断。
- 只有明显低风险、无路径、无网络、无状态变更的工具可以在工具级返回 `allow`。
- 新工具必须声明 `permission_category`，未声明或无法识别时注册失败。
- `state_effect="none"` 是并发调度的必要条件。
- `is_concurrency_safe()` 可以进一步按输入收紧，但不能把有状态工具放宽为并发。
- `validate_input()` 只做输入语义校验，不负责最终权限放行。
- 未知工具、schema 错误、禁用工具和权限无法判断都返回 `ToolRunResult(is_error=True)`。
- Tool 系统不定义 `ToolResultBlock`，也不直接构造 `UserMessage` 或 API content block；这些都由 Context 系统统一处理。

## 4. ToolExecutionContext

`ToolExecutionContext` 是工具执行共享上下文。它只包含工具执行必需的权限、路径、文件快照、取消信号和会话标识，不包含 `messages`、`context_messages`、`api_messages`、attachment 或模型上下文预算。

```py
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Literal


PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]


@dataclass
class PermissionRule:
    tool_name: str
    behavior: Literal["allow", "deny", "ask"]
    pattern: str | None = None
    source: str = "session"


@dataclass
class ToolPermissionContext:
    mode: PermissionMode = "default"
    always_allow: list[PermissionRule] = field(default_factory=list)
    always_deny: list[PermissionRule] = field(default_factory=list)
    always_ask: list[PermissionRule] = field(default_factory=list)
    should_avoid_permission_prompts: bool = False


@dataclass
class ToolExecutionContext:
    cwd: Path
    workspace_roots: list[Path]
    permission_context: ToolPermissionContext
    read_file_state: "ReadFileState"
    abort_event: Event
    session_id: str
    hook_bus: "HookBus | None" = None
    is_non_interactive_session: bool = False
```

启动时必须完成：

- `cwd = cwd.resolve(strict=True)`。
- `workspace_roots` 中每个根目录都必须 `resolve(strict=True)`。
- 去重后保留真实路径，不保留用户输入的相对路径。
- 如果某个 workspace root 不存在，启动失败或要求用户重新配置。

## 5. 路径规范化

路径权限判断必须基于真实路径，而不是字符串前缀。

规范化规则：

- 已存在路径：使用 `path.resolve(strict=True)` 得到真实路径。
- 新建文件：先解析父目录，最终路径使用 `parent.resolve(strict=True) / name`。
- 新建目录：先解析最近存在的父目录，并逐级检查中间路径不能通过 symlink 穿透到外部。
- 不允许 `..`、相对路径或 symlink 在权限判断后改变目标范围。
- 写入前必须重新解析父目录和已存在目标。
- 如果目标已存在，按目标真实路径检查权限。
- 如果目标不存在，按真实父目录和最终文件名检查权限。
- workspace 内 symlink 指向外部时，读取和写入都按外部真实路径处理。
- 新建文件禁止通过 workspace 内 symlink 目录穿透到外部。

建议返回结构：

```py
@dataclass
class ResolvedPath:
    requested: Path
    resolved: Path
    exists: bool
    parent_resolved: Path
    inside_workspace: bool
    workspace_root: Path | None
    is_symlink_escape: bool
```

workspace 判断使用 `Path.is_relative_to(root)` 或等价逻辑，且 `root` 和 `resolved` 都必须是真实路径。

## 6. 权限模型

### 6.1 权限模式

系统支持四种权限模式：

- `default`：读 workspace 默认允许；workspace 外读默认询问；写入、删除、危险命令、网络、MCP、Agent、Skill 默认询问或按工具约束处理。
- `acceptEdits`：允许 workspace 内常规编辑和写入；仍然拦截硬拒绝、危险 shell、外部写入、敏感文件、网络和权限提升。
- `plan`：不允许改变 workspace、外部状态或远程状态；只允许读工具、只读 Bash、只读规划子 Agent、`WritePlan`、`PlanShow`、`AskUserQuestion`、`ExitPlanMode` 等必要规划工具。
- `bypassPermissions`：跳过普通询问，但不能绕过 hard deny；系统路径、凭据文件、大范围删除、权限提升、SSRF、未注册 Skill、越权 Agent 仍然拒绝。

### 6.2 硬拒绝

hard deny 在所有模式下优先，不能被显式 allow 或 `bypassPermissions` 覆盖。

硬拒绝包括：

- 写入或删除系统关键路径，例如 `/`, `/bin`, `/sbin`, `/usr`, `/etc`, `/var`, `/boot`, `/dev`, `/proc`, `/sys`。
- 读取或写入明确的凭据文件，例如 `.env`, `.npmrc`, `.pypirc`, `.netrc`, SSH 私钥、云厂商 credential 文件。
- 大范围删除，例如 `rm -rf /`、删除 workspace root、删除用户 home、通配符删除父级目录。
- 权限提升，例如 `sudo`、`su`、修改系统服务、修改 shell 启动文件以持久化命令。
- `bypassPermissions` 下的外部写入、外部删除和危险网络目标。
- Web 请求访问 localhost、内网 IP、metadata IP、`file://` 或非 `http/https` 协议。
- MCP server 配置不合法、试图绕过网络 / 本地进程权限、Skill 名称不合法、路径不在注册目录、试图执行任意本地文件。
- 子 Agent 请求扩大 workspace、提升权限模式或覆盖父级 hard deny。

### 6.3 统一权限顺序

权限引擎必须使用固定顺序，避免规则歧义。`PreToolUse` hooks 在进入权限引擎之前执行，用来阻断或改写输入；改写后的输入必须重新 schema 校验、重新构造权限目标，再进入下面顺序。

```txt
0. PreToolUse hook aggregate
1. hard deny
2. explicit deny
3. explicit ask
4. explicit allow
5. tool constraints
6. mode default policy
7. non-interactive normalization
```

含义：

- `hard deny`：不可绕过安全边界。
- `explicit deny`：用户或配置明确拒绝。
- `explicit ask`：用户或配置明确要求每次询问。
- `explicit allow`：用户或配置明确允许，但不能覆盖 hard deny 和工具约束。
- `tool constraints`：工具自身的必需限制，例如 `Edit` 必须先读、`WebFetch` 禁止内网。
- `mode default policy`：根据 `default`、`acceptEdits`、`plan`、`bypassPermissions` 给出默认行为。
- `non-interactive normalization`：非交互环境中，最终 `ask` 必须转为 `deny`，不能静默转 `allow`。
- `PreToolUse hook aggregate`：hook 返回 `block` 时直接拒绝；返回 `ask` 时作为后续交互提示的来源；返回 `approve` 不能覆盖后续 hard deny、显式 deny、工具约束、Plan Mode 或非交互归一化。

伪代码：

```py
async def decide_permission(tool, input, ctx):
    target = build_permission_target(tool, input, ctx)

    hard = check_hard_deny(target, ctx)
    if hard:
        return deny(hard.reason)

    rule = match(ctx.permission_context.always_deny, target)
    if rule:
        return deny(rule.reason)

    rule = match(ctx.permission_context.always_ask, target)
    if rule:
        decision = ask(rule.reason)
    else:
        rule = match(ctx.permission_context.always_allow, target)
        if rule:
            decision = allow(rule.reason)
        else:
            tool_decision = await tool.check_permissions(input, ctx)
            if tool_decision.behavior in ("deny", "ask", "allow"):
                decision = tool_decision
            else:
                decision = apply_mode_default(tool, input, ctx)

    decision = apply_tool_constraints(decision, tool, input, ctx)
    decision = normalize_non_interactive(decision, ctx)
    return decision
```

实现时要保持实际顺序和上面的固定顺序一致。工具约束不能被显式 allow 绕过；因此如果伪代码先得到 allow，也必须在 `apply_tool_constraints()` 中重新收紧。

Tool Runner 负责把 `PreToolUse` hook 聚合结果转换为权限流程输入；`permissions.py` 不直接执行 hooks，避免权限引擎和生命周期总线互相递归。

### 6.4 读写默认策略

读取：

- workspace 内普通文件读取：`default` 允许。
- workspace 外读取：`default` 询问；非交互转拒绝。
- 凭据文件读取：hard deny。
- 大文件读取必须受执行侧输出上限保护；完整快照仍用于写入一致性校验。

写入：

- workspace 内新建文件：`default` 询问，`acceptEdits` 可允许。
- workspace 内覆盖已有文件：必须结合 `ReadFileState`；未读过目标时 `default` 询问，`acceptEdits` 可允许但提示“未读过目标文件”。
- workspace 外写入：默认询问；非交互拒绝；`bypassPermissions` 也不能写系统路径或敏感路径。
- 删除：默认询问；删除目录、递归删除、通配符删除默认提高风险等级。
- plan 模式：拒绝所有 workspace 写入和外部写入。

非交互处理：

- `should_avoid_permission_prompts=True` 时，所有最终 `ask` 归一化为 `deny`。
- 自动任务需要继续执行时，必须依赖明确 allow 规则或更合适的权限模式。
- 不允许因为非交互而静默 allow。

## 7. ReadFileState

`ReadFileState` 有两个职责：防止重复读取同一文件范围导致上下文膨胀，以及防止编辑覆盖外部修改。它不是普通文件缓存，而是读视图状态和写入安全边界的一部分。

### 7.1 快照内容

每次 `Read` 成功读取普通文本或 notebook 文件后记录读视图快照：

```py
@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    exists: bool
    mtime_ns: int | None
    size: int | None
    sha256: str | None
    content_digest: str | None
    offset: int | None
    limit: int | None
    view_kind: Literal["full", "range", "summary", "partial"]
    source: Literal["read", "write", "auto_inject", "summary"]
    is_partial_view: bool
    read_at: float


@dataclass(frozen=True)
class DuplicateReadHit:
    path: Path
    offset: int | None
    limit: int | None
    message: str = "File content for this range is already in context and unchanged on disk."


class ReadFileState:
    def record_read(self, path: Path, snapshot: FileSnapshot) -> None: ...
    def get_snapshot(self, path: Path) -> FileSnapshot | None: ...
    def check_duplicate_read(
        self,
        path: Path,
        offset: int | None,
        limit: int | None,
    ) -> DuplicateReadHit | None: ...
    def validate_unchanged(self, path: Path) -> None: ...
    def refresh_after_write(self, path: Path) -> None: ...
    def lock_for(self, path: Path) -> "Lock": ...
```

`path` 必须是真实路径。hash 建议使用完整 `sha256`；如果为了性能对大文件做分段 hash，必须在快照中标记算法，并在校验时使用同一算法。`content_digest` 是本次返回给模型的视图内容摘要，用于审计和调试，不替代完整文件一致性校验。

`ReadFileState` 应使用 LRU 限制内存：

- 按条目数量限制，例如最近 100 个文件视图。
- 按缓存内容总字节数限制，例如 25 MB。
- LRU 淘汰只降低重复读命中率，不能降低权限、路径和写入一致性检查的安全性。
- 图片、PDF、二进制文件和超大落盘摘要默认不缓存完整内容，也不参与重复读去重。

### 7.2 读写规则

- `Read`：读取前先检查重复读；读取后记录快照；如果文件过大被截断，也要对完整文件建立快照，但截断视图标记为 `is_partial_view=True`。
- `Read`：只有同一真实路径、同一 `offset/limit`、快照来源是 `source="read"`、非 partial view、文件 `mtime_ns/size/sha256` 未变化时，才可以返回重复读命中。
- `Read`：重复读命中时返回轻量结果，例如 `file_unchanged` 或 `already_read`，提示该范围内容已在上下文中且磁盘未变，不再重复返回完整内容。
- `Read`：range 不同、文件变化、快照来自 `write` / `auto_inject` / `summary`、或缓存已淘汰时，必须重新读取。
- `Edit`：必须先有同一真实路径的读取快照；没有快照直接拒绝或要求先 `Read`。
- `Edit`：写入前校验当前 `mtime_ns`、`size`、`sha256` 与快照一致。
- `Write` 新建文件：不要求已有快照，但要确认目标不存在且父目录权限允许。
- `Write` 覆盖已有文件：默认模式要求询问；更严格实现可以要求先 `Read`。
- `acceptEdits` 覆盖未读文件：可允许，但权限提示必须明确“未读过目标文件”，并记录审计事件。
- 写入成功后必须刷新快照，使后续编辑基于新内容。
- 如果文件被删除，`Edit` 拒绝；`Write` 按新建文件流程重新判断。
- 如果 `mtime_ns` 变化但 hash 相同，可以刷新快照后继续；如果 hash 或 size 变化，视为外部修改并拒绝覆盖。
- 如果 hash 变化但 mtime 未变化，同样按外部修改处理。
- 写后刷新快照的 `source` 必须是 `write`，不能让下一次 `Read` 错误复用写入前的旧内容；下一次 `Read` 应重新返回当前内容并记录新的 `source="read"` 快照。

### 7.3 重复读去重

重复读去重只优化上下文，不改变权限判断，也不改变编辑前必须已读的要求。

去重命中条件：

- 请求路径解析到同一真实路径。
- 请求的 `offset` 和 `limit` 与快照完全一致。
- 快照来源是 `source="read"`，说明模型已经通过 Read 看过该视图。
- `is_partial_view=False`，不能把自动注入、摘要、截断或部分视图当成完整上下文。
- 当前磁盘状态与快照一致，至少比较 `mtime_ns` 和 `size`，高风险或异常场景再比较 `sha256`。
- 文件类型是文本或 notebook 这类可稳定复用的内容。

不能去重的情况：

- 图片、PDF、二进制文件。
- `.tool-results/` 摘要读取或落盘引用。
- 自动注入的上下文文件、Skill 触发加载、启动注入内容。
- `Edit` / `Write` 写后刷新产生的快照。
- offset、limit、编码、换行归一化策略不同。
- 文件 stat 失败、文件被删除、mtime/size/hash 任一关键字段不可信。

重复读命中返回轻量结果：

```py
ToolResult(data={
    "type": "file_unchanged",
    "file_path": requested_path,
    "message": "This file range is already in context and unchanged on disk.",
})
```

Runner 映射结果时应给模型一个明确 stub，而不是空结果。stub 必须说明内容已在上下文中，避免模型误以为读取失败。

### 7.4 文件级锁

并发写入必须在同一个文件级锁内完成“检查、写入、刷新快照”。

```py
async with ctx.read_file_state.lock_for(resolved_path):
    ctx.read_file_state.validate_unchanged(resolved_path)
    await atomic_write(resolved_path, new_content)
    ctx.read_file_state.refresh_after_write(resolved_path)
```

锁要求：

- 锁 key 使用真实路径。
- `Edit` 和 `Write` 都必须使用同一套锁。
- 同一文件并发写必须串行。
- 不同文件可以并发，但仍要遵守工具状态和权限规则。
- 原子写建议使用同目录临时文件加 `replace`，并保持权限和换行策略。

## 8. 并发调度

默认调度策略：

- `Agent` 默认不并发，除非显式隔离状态、权限和输出。
- `Bash` 默认不并发，除非命令被明确归类为无状态只读。
- `Read` 会更新 `ReadFileState`，不应仅因为“读取文件”就自动并发；如需并发读取，必须由工具内部持锁处理快照更新。
- `Glob`、`Grep` 等纯搜索工具可以在 `state_effect="none"` 且不写输出文件时并发。
- `WebFetch` 有网络副作用和 SSRF 风险，默认串行；如需并发必须加域名、速率和结果预算限制。
- `Todo`、Task 工具和 Plan Mode 工具修改 app state，默认串行。

调度器规则：

```py
def can_run_parallel(tool, input, ctx):
    return (
        tool.state_effect == "none"
        and tool.is_concurrency_safe(input, ctx)
        and not ctx.abort_event.is_set()
    )
```

一个 tool use batch 中：

- 先分离可并发的 `state_effect="none"` 工具。
- 有状态工具按模型顺序串行执行。
- 任一工具触发取消时，停止启动新工具。
- 并发结果按原始 tool use 顺序返回。
- 每个结果单独应用执行侧输出上限，batch 总输出再做整体执行侧上限保护。

## 9. Bash 工具

`Bash` 的风险来自 shell 语法复杂度，不能只靠关键词判断只读。

默认策略：

- 简单白名单命令可以按只读处理，例如 `pwd`、`ls`、`find`、`rg`、`grep`、`cat`、`sed -n`、`git status`、`git diff`、`git show`。
- 出现复杂 shell 语法时默认 `ask`：`&&`、`;`、`||`、`$()`、反引号、重定向、管道、后台执行、进程替换、通配符删除。
- 出现写入或状态改变命令时默认 `ask` 或 `deny`：`rm`、`mv`、`cp` 覆盖、`chmod`、`chown`、`git checkout`、`git reset`、`npm install`、包管理器安装、数据库迁移、部署命令。
- 出现权限提升时 hard deny：`sudo`、`su`、修改系统服务。
- `plan` 模式下只允许白名单只读命令。
- 非交互环境中复杂命令的 `ask` 转为 `deny`。

建议解析流程：

```txt
1. 使用 shell 解析器解析命令。
2. 解析失败则 ask。
3. 如果 AST 含复杂控制结构，则 ask。
4. 如果是单命令且命令名在只读白名单，再检查参数。
5. 参数含输出、删除、安装、网络、权限提升时收紧。
6. 无法证明只读时 ask。
```

## 10. 文件工具

### Read

- 只读取普通文件。
- 目录读取交给 `Glob` 或专门目录列表工具。
- 读取前先调用 `ReadFileState.check_duplicate_read()`；命中时返回 `file_unchanged` / `already_read` 轻量结果，不重复返回完整内容。
- 对大文件按执行侧输出上限截断返回，但快照覆盖完整文件；截断或摘要视图不能参与重复读去重。
- 成功返回文本或 notebook 内容后记录 `source="read"` 快照，包含真实路径、range、文件状态和视图摘要。
- 图片、PDF、二进制文件和 `.tool-results/` 摘要读取不参与重复读去重。

### Edit

- 输入包含 `file_path`、`old_string`、`new_string`、可选 `replace_all`。
- 必须先读取同一真实路径。
- `old_string` 不存在或多次出现且未设置 `replace_all` 时拒绝。
- 写入前在文件锁内校验快照。
- 写入后刷新快照。

### Write

- 输入包含 `file_path`、`content`。
- 新建文件按父目录权限判断。
- 覆盖已有文件按 `ReadFileState` 策略处理。
- 默认模式下覆盖未读文件必须询问。
- `acceptEdits` 可以允许 workspace 内覆盖未读文件，但要提示并审计。
- 写入采用原子替换，失败时不能留下半写文件。

## 11. Web 工具

`WebFetch` 和 `WebSearch` 必须经过网络权限判断。

`WebFetch` 规则：

- 只允许 `http` 和 `https`。
- 拒绝 `file`、`ftp`、`gopher`、`data`、`javascript` 等协议。
- 解析 DNS 后拒绝 localhost、loopback、link-local、private、multicast 和 metadata IP。
- 默认拒绝 `169.254.169.254` 以及云厂商 metadata 域名。
- 限制跳转次数；每次跳转后重新做 SSRF 检查。
- 限制响应大小、MIME 类型和下载时间。
- 默认不发送本地凭据、cookie、代理认证或环境中的 token。

`WebSearch` 规则：

- 搜索查询经过预算限制。
- 结果只返回标题、摘要、URL 和必要元数据。
- 打开结果仍通过 `WebFetch` 权限和 SSRF 检查。

## 12. MCP-backed 工具

MCP-backed 工具是普通 BigCode `BaseTool` 的一种后台路由。对模型来说，它和本地 `Read`、`WebFetch`、`SkillLoad` 等工具没有协议差异；模型只看到工具名、描述和 schema。MCP server name、真实 MCP tool name、transport 和连接状态只存在于 Tool Registry 的 route metadata 和权限目标中。

核心规则：

- MCP 配置、FastMCP client 生命周期、tools/resources/prompts discovery 以 `MCP-Skill.md` 为准。
- Tool Registry 把 MCP server 的 tool 动态注册为普通工具名，并在内部 route metadata 中记录 `server_name` 和真实 `tool_name`。
- 模型调用这些普通工具时，BigCode 仍先执行 schema 校验、权限判断、输出上限和错误映射；Runner 根据 route metadata 自动调用 FastMCP `call_tool()`。
- MCP resource 和 prompt 不自动进入上下文，必须通过 `ExternalResourceRead` / `ExternalPromptGet` 等受控工具显式读取。
- MCP-backed 普通工具的 `readOnlyHint` 可以作为权限收窄参考，但不能覆盖 hard deny、显式 deny、Plan Mode 和非交互策略。
- MCP server 返回的文本、prompt、resource 都是外部不可信上下文，不能覆盖 BigCode system prompt、权限规则或用户指令。
- MCP stdio server 启动本地进程，按本地命令执行面处理；HTTP / SSE server 按网络访问处理，并复用 SSRF 防护。

推荐 Tool：

| 工具 | 权限类别 | 状态影响 | 用途 |
|------|----------|----------|------|
| discovered MCP-backed 普通工具 | `mcp` | `external` | 以普通工具名调用外部 MCP server 的具体 tool |
| `ExternalResourceList` | `mcp` | `external` | 列出某个外部能力来源的 resources / templates |
| `ExternalResourceRead` | `mcp` | `external` | 显式读取某个外部 resource |
| `ExternalPromptList` | `mcp` | `external` | 列出某个外部能力来源的 prompts |
| `ExternalPromptGet` | `mcp` | `external` | 获取某个外部 prompt，返回受控 ToolResult，由 Context 渲染为 meta reminder |

权限默认：

- `default`：MCP connect、未知副作用 tool 调用默认 ask。
- `plan`：只允许 discovery、prompt get、resource read 和明确 read-only 的 MCP-backed 普通工具；其他 MCP-backed 普通工具默认 deny。
- `bypassPermissions`：跳过普通询问，但不能绕过本地进程、网络 SSRF、敏感路径、凭据和 hard deny。
- 非交互：所有最终 ask 转 deny，除非配置或规则显式 allow。

## 13. Skill 工具

Skill 只能来自注册目录，不能执行任意文件。

规则：

- Skill 名称只能匹配白名单格式，例如 `^[a-z0-9][a-z0-9_-]{0,63}$`。
- 启动时扫描注册目录，生成 name 到真实路径的映射。
- 调用时只按注册表查找，不接受模型传入路径。
- Skill 文件必须位于注册目录真实路径下，symlink 指向外部时拒绝。
- Skill 内容只能作为受控指令或资源加载；v1 不执行 Skill 脚本。
- Skill 不能改变当前权限模式，不能新增 allow 规则，不能扩大 workspace。
- `SkillLoad` 只返回 `SKILL.md` 正文和资源清单，不自动读取所有资源。
- `SkillResourceRead` 只读取 skill 根目录内的相对路径资源，并拒绝绝对路径、`..`、凭据文件和 symlink 外跳。
- Skill 内容进入 Context 时属于外部能力上下文，不能覆盖 system prompt 或权限规则。

推荐 Tool：

| 工具 | 权限类别 | 状态影响 | 用途 |
|------|----------|----------|------|
| `SkillLoad` | `skill` | `external` | 按注册名称加载 `SKILL.md` |
| `SkillResourceRead` | `skill` | `external` | 读取已注册 Skill 的附属资源 |

## 14. Agent 工具

子 Agent 是权限继承对象，不是新的权限主体。

本节只定义 Tool 系统看到的 `AgentTool` 边界：权限继承、Runner 接入、执行侧上限和 `ToolRunResult` 返回。`AgentDefinition`、`SubAgentContext`、工具池过滤、后台 task、sidechain transcript 等内部设计以 `subAgent.md` 为准。

规则：

- 子 Agent 必须继承父级 `permission_context`、`cwd`、`workspace_roots`、hard deny 和非交互设置。
- 子 Agent 不能提升权限模式，不能扩大 workspace，不能禁用 hard deny。
- 在 `plan` 权限模式下，只允许启动只读规划类子 Agent，例如 `explorer` 和 `planAgent`；它们不得写文件、调用 `ExitPlanMode`、再启动子 Agent 或运行非只读命令。
- Python v1 默认让 subAgent clone 父级 `ReadFileState`，隔离读取预算和缓存；subAgent 写入成功后必须同步目标文件快照回父级，保证父级后续编辑不会误判。
- 子 Agent 默认串行执行。
- 子 Agent 输出使用独立执行侧上限，汇总给父 Agent 时再做二次上限保护。

## 15. App State 工具

App State 工具修改 BigCode 自身状态，不直接修改 workspace 文件。它们仍然必须经过 Tool registry、schema 校验、权限模式和串行调度。

v1 需要实现的 Task 工具：

| 工具 | 状态影响 | 用途 |
|------|----------|------|
| `TaskCreate` | 写 app state | 创建 `pending` 任务，记录 subject、description、active form 和 metadata |
| `TaskUpdate` | 写 app state | 更新任务状态、owner、说明和依赖关系 |
| `TaskList` | 读 app state | 查看当前 task list 摘要，避免重复创建任务或误判进度 |
| `TaskGet` | 读 app state | 查看单个任务完整详情，用于恢复上下文或处理复杂依赖 |

Task 工具规则：

- Task 文件存储、锁、ID 分配、依赖和 claim 行为以 `TaskPlan.md` 为准。
- `TaskCreate` / `TaskUpdate` 默认串行执行。
- `TaskList` / `TaskGet` 是读取接口，但仍读取 app state；Runner 可以串行处理，避免和同批次状态写入出现读写顺序歧义。
- Plan Mode 中不要要求模型为了规划本身创建 Task；Task 主要用于 approved plan 之后的执行进度追踪。

Plan 工具规则：

- `EnterPlanMode` / `ExitPlanMode` 修改 app state，必须串行。
- `WritePlan` 是 plan 模式下唯一允许的写入口，只能写当前 plan 文件，不能作为任意文件写入工具；`WritePlanInput` 只是该工具的输入 schema 名。
- `PlanShow` 只读取当前 plan path/content，不修改状态，不触发审批，主要服务 `/plan` 或调试展示。
- `plan` 权限模式下工具白名单为读工具、只读 Bash、只读规划子 Agent、`WritePlan`、`PlanShow`、`AskUserQuestion`、`ExitPlanMode` 和必要本地 plan 命令；其他写入、网络写、安装、服务启动、提交、删除和实现型 Agent 默认拒绝。
- Plan Mode 的权限以 `TaskPlan.md` 和本文件为准；Plan reminder、approved plan reminder 和 Plan Stop 约束通过 `Hooks.md` 的内置 hooks 进入 Context。

v1 需要实现的 Plan / 交互工具：

| 工具 | 权限类别 | 状态影响 | 用途 |
|------|----------|----------|------|
| `EnterPlanMode` | `state` | `app_state` | 保存原权限模式，切换到 `plan`，生成或复用当前 plan 文件 |
| `WritePlan` | `state` | `app_state` | 覆盖当前 session plan 文件，是 plan 模式唯一允许的写入口 |
| `PlanShow` | `state` | `app_state` | 返回当前 plan path 和内容，不请求审批 |
| `ExitPlanMode` | `state` | `app_state` | 展示 plan 并请求审批，批准后恢复原权限模式并保存 approved plan |
| `AskUserQuestion` | `state` | `app_state` | 暂停当前 turn，向用户澄清需求、偏好或方案取舍 |

Plan 工具失败行为：

- 当前不在 plan mode 时，`WritePlan` 和 `ExitPlanMode` 返回明确工具错误。
- 当前没有 plan 文件或 plan 内容为空时，`ExitPlanMode` 返回明确工具错误。
- 用户拒绝 plan 时，`ExitPlanMode` 保持 `plan` 模式，并把反馈作为下一轮用户输入进入上下文。
- 非交互环境没有审批通道时，`ExitPlanMode` 返回 `requires_approval` 或拒绝退出，不能静默批准。

`AskUserQuestion` 是交互型 app state 工具：它不修改 workspace，但会暂停当前 agent turn 等待用户输入；Runner 必须把它作为串行、需要用户交互的工具处理。

## 16. 输出预算和落盘

Tool 系统只负责执行侧输出上限，目标是防止进程内存、日志和 IPC 数据无限增长。模型上下文里的 `tool_result` 截断、落盘引用、`tool_result_reference` attachment 和媒体保留策略由 Context 系统负责。

规则：

- 单次工具执行必须有最大原始输出字节数。
- Bash stdout/stderr、网页正文、搜索结果和文件内容分别有执行侧读取上限。
- 工具超过执行侧上限时，Runner 返回截断后的 `ToolRunResult`，并在 `metadata` 标记 `truncated=True`、原始大小和可选临时文件路径。
- Tool 系统不决定这些内容如何进入模型上下文；Context 系统拿到 `ToolRunResult` 后再转换成 `ToolResultBlock` 并应用上下文预算。

## 17. Runner 流程

工具执行主流程：

```txt
1. 接收模型 tool_use。
2. 在 registry 中查找工具，未知工具返回错误。
3. 用 pydantic 校验输入，失败返回错误。
4. 检查工具是否启用。
5. 执行 validate_input。
6. 触发 `HookBus.emit("PreToolUse")`。
7. 如果 hook 阻断，返回权限/工具错误；如果 hook 改写输入，重新执行 schema 校验和 validate_input。
8. 构造权限目标，调用统一权限引擎。
9. 如果 deny，返回权限错误。
10. 如果 ask，向用户请求确认；非交互下 ask 已经转 deny。
11. 根据 state_effect 和 is_concurrency_safe 调度。
12. 执行 call，支持 progress 和 abort。
13. 将成功或失败包装为 `ToolRunResult`。
14. 应用执行侧输出上限。
15. 触发 `HookBus.emit("PostToolUse")`，收集附加上下文、hook 错误或阻断信息。
16. 按原 tool_use 顺序返回 `ToolRunResult[]` 给 Context 系统。
```

Runner 不应让工具绕过以下步骤：

- schema 校验
- `PreToolUse` / `PostToolUse` 生命周期点
- 统一权限引擎
- hard deny
- 执行侧输出上限
- 取消信号
- 错误结果映射

## 18. 设计自检循环

固定闭环：

1. 列出系统目标和边界。
2. 按模块检查风险：权限、路径、并发、文件一致性、Bash、Web、MCP、Agent、Skill、输出预算、非交互。
3. 对每个风险写入明确修复策略。
4. 给每个修复绑定测试场景。
5. 只有所有风险都有修复和测试，才标记为“策略已闭环”。

当前风险闭环：

| 风险 | 修复策略 | 验收测试 |
| --- | --- | --- |
| `BaseTool.check_permissions` 默认 allow | 默认返回 `passthrough`，统一权限引擎负责最终判断；新工具必须声明权限类别 | 注册未声明权限类别的工具应失败；默认工具级决策不得直接 allow 写入 |
| 权限顺序歧义 | 固定为 hard deny、显式 deny、显式 ask、显式 allow、工具约束、模式默认、非交互归一化 | 构造冲突规则，验证 hard deny 和工具约束优先 |
| Bash 只读误判 | 复杂 shell 语法默认 ask；解析失败默认 ask；无法证明只读默认 ask | `ls && rm x`、`cat a > b`、`echo $(rm x)` 不得被当作只读 |
| `bypassPermissions` 过宽 | hard deny 不可绕过，敏感路径、系统路径、SSRF、权限提升仍拒绝 | `bypassPermissions` 下写 `/etc/hosts`、访问 metadata IP、执行 `sudo` 应拒绝 |
| 并发工具改状态 | 只有 `state_effect="none"` 可并发；有状态工具串行 | Todo、Write、Agent 在同 batch 中保持串行 |
| `ReadFileState` 并发竞态 | 文件级锁内完成检查、写入、刷新快照 | 两个 Edit 同时写同一文件时只能串行且不丢更新 |
| 重复读导致上下文膨胀 | `ReadFileState` 记录 read 视图，未变的同 range 重复读返回轻量 stub | 同文件同 range 第二次 Read 不重复返回全文 |
| 错误去重导致看不到新内容 | 只有 `source="read"`、非 partial view、同 range、磁盘未变才去重 | 文件变化、写后刷新、partial view、range 不同都必须重新读取 |
| 不存在路径解析不一致 | 新建文件使用 `parent.resolve() / name` | 新建文件路径含 `..` 和 symlink 父目录时按真实父目录判断 |
| symlink 写入绕过 | 已存在目标按真实路径检查；新建文件禁止通过 symlink 目录穿透 | workspace 内 symlink 指向外部时写入应按外部路径处理或拒绝 |
| Write 覆盖无快照 | default 覆盖未读文件 ask；acceptEdits 可允许但提示并审计；严格实现可要求先 Read | 未读覆盖在 default 下不能静默写入 |
| 输出过大 | Tool 侧只做执行输出上限；进入模型上下文前的落盘引用由 Context 处理 | 大 stdout 不得撑爆进程内存，返回结果必须标记截断元数据 |
| 非交互 ask 阻塞 | ask 统一转 deny；通过显式 allow 解决自动化 | 非交互外部读写返回 deny，不等待输入 |
| WebFetch SSRF | 仅允许 http/https；拒绝 localhost、内网、metadata 和跳转后危险地址 | 请求 `http://127.0.0.1`、metadata IP、跳转到内网都拒绝 |
| MCP 变成权限旁路 | MCP connect / call_tool / resource / prompt 都映射成 Tool 权限目标；stdio 按本地进程处理，HTTP/SSE 按网络处理 | default 下 MCP connect ask；非交互转 deny；plan mode 未知副作用 MCP-backed 普通工具拒绝 |
| MCP prompt injection | MCP 返回内容只作为 tool_result 或 meta reminder，不覆盖 system prompt，并标记外部不可信来源 | MCP prompt 中包含“忽略系统提示”时仍不能改变权限或 system prompt |
| Skill 注入 | 只从注册目录按白名单名称加载，不接受任意路径 | `../x`、绝对路径、symlink 外跳应拒绝 |
| Agent 权限继承 | 子 Agent 继承父权限和 workspace，不能提升；clone 父级 `ReadFileState` 并在写入后同步父级快照 | 子 Agent 请求 `bypassPermissions` 或外部 workspace 应拒绝；子 Agent 写入后父级快照可见 |

闭环结论：在当前设计范围内，所有已识别的高风险路径都有对应防护和验收测试。实际信心来自后续实现中这些测试全部通过，而不是主观保证。

## 19. 实现验收标准

后续写代码时必须至少覆盖以下测试。

权限测试：

- hard deny 优先于显式 allow。
- 显式 deny 优先于 ask 和 allow。
- 显式 ask 优先于 allow。
- 工具约束能收紧显式 allow。
- 非交互下 ask 转 deny。
- 四种权限模式分别覆盖读、写、删除、Bash、Web、MCP、Agent、Skill。

路径测试：

- workspace 内路径允许按模式执行。
- workspace 外路径默认 ask，非交互 deny。
- `..` 不得逃逸 workspace。
- symlink 指向外部时按真实路径判断。
- 不存在文件使用父目录真实路径判断。
- 敏感文件读取和写入 hard deny。

文件一致性测试：

- `Edit` 未先 `Read` 拒绝。
- `Read` 后文件被外部修改，`Edit` 拒绝。
- `Read` 后文件被删除，`Edit` 拒绝。
- `Write` 覆盖未读文件在 default 下 ask。
- 写入成功后刷新快照。
- 并发编辑同一文件不丢更新。
- 同一文件同一 `offset/limit` 且磁盘未变时，第二次 `Read` 返回轻量 unchanged 结果。
- 同一文件不同 `offset/limit` 必须重新读取对应范围。
- 文件内容、mtime、size 或 hash 变化后，重复 `Read` 必须重新返回内容并刷新状态。
- partial view、自动注入、摘要读取、图片、PDF、写后刷新状态不能触发重复读去重。
- LRU 淘汰后再次 `Read` 应正常返回完整内容。
- 重复读命中不影响 `Edit` 的“必须先 Read 且文件未变”校验。

Bash 测试：

- 白名单只读命令允许。
- `&&`、`;`、`||`、管道、重定向、命令替换默认 ask。
- `sudo` hard deny。
- 删除、安装、部署类命令默认 ask 或 deny。
- plan 模式拒绝写状态命令。

Web 测试：

- `https` 普通公网 URL 可按权限策略请求。
- `file://` 拒绝。
- localhost、内网 IP、metadata IP 拒绝。
- 跳转到危险地址拒绝。
- 大响应被截断或落盘。

MCP 测试：

- 用户级和项目级 MCP 配置合并，同名 server 项目级覆盖。
- FastMCP discovery 能把 tools/resources/prompts 转成 capability index。
- discovered MCP-backed 普通工具的 route metadata 映射到正确 server 和真实 tool name。
- MCP-backed 普通工具 error 映射为 `ToolRunResult(is_error=True)`。
- plan mode 拒绝未知副作用 MCP-backed 普通工具，只允许明确 read-only tool、resource read 和 prompt get。
- stdio MCP connect 按本地进程权限处理，HTTP/SSE MCP connect 按网络权限处理。
- MCP 大结果被截断或落盘。

并发测试：

- 只有 `state_effect="none"` 的工具进入并发队列。
- 有状态工具按模型顺序执行。
- 并发结果按原始 tool use 顺序返回。
- 取消信号能阻止后续工具启动。

执行输出上限测试：

- 大文件读取被截断但快照完整。
- 大 stdout 不得撑爆进程内存，Runner 返回截断元数据。
- batch 总输出超限时按执行侧上限裁剪；上下文级二次裁剪交给 Context 测试覆盖。

Agent、MCP 和 Skill 测试：

- 子 Agent 不能提升权限模式。
- 子 Agent 不能扩大 workspace。
- 子 Agent 写入后父级 `ReadFileState` 可见。
- 子 Agent 不能新增 MCP server，后台子 Agent 没有显式 allow 时不能发起交互授权型 MCP 调用。
- Skill 名称必须符合白名单。
- Skill symlink 外跳拒绝。
- Skill 不能新增权限 allow 规则。

## 20. 实施顺序

推荐按以下顺序实现，便于每一步都有可验证结果：

1. `paths.py`：真实路径解析、workspace 判断、symlink 防穿透。
2. `permissions.py`：权限模式、规则匹配、hard deny、非交互归一化。
3. `base.py` 和 `registry.py`：工具协议、权限类别、注册校验。
4. `output_limits.py`：工具执行阶段输出上限。
5. `read_file_state.py`：快照、hash、文件级锁。
6. `Read`、`Edit`、`Write`：先完成文件闭环。
7. `Glob`、`Grep`：纯搜索工具和并发调度。
8. `Bash`：shell 解析和风险分类。
9. `WebFetch`、`WebSearch`：网络权限和 SSRF 防护。
10. `TaskCreate`、`TaskUpdate`、`TaskList`、`TaskGet`、`EnterPlanMode`、`WritePlan`、`PlanShow`、`ExitPlanMode`、`AskUserQuestion`：app state 工具。
11. `Skill`：注册目录、名称白名单、`SkillLoad` 和 `SkillResourceRead` 受控加载。
12. `MCP`：FastMCP Client 配置、discovery、普通工具动态注册、resource / prompt 读取。
13. `Agent`：权限继承、状态共享、执行输出上限，并收窄 MCP / Skill 工具池。
14. `runner.py`：完整 tool_use 执行、并发、错误映射和执行侧输出上限。

每完成一层，都先补验收测试，再接下一层。不要先实现宽松版本再补安全边界；权限和路径是基础模块，后补容易留下绕过路径。
