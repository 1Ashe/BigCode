# SubAgent 系统设计（Python 版）

## 总纲

SubAgent 系统的目标是用 Python 复现 Claude Code 的核心子代理业务链路：

```txt
父 agent 调用 AgentTool
  -> 选择 AgentDefinition
  -> 构造独立 SubAgentContext
  -> 运行一条独立 agent loop
  -> 汇总最后结果
  -> 作为 AgentTool 的 tool_result 交还父 agent
```

BigCode 的 subAgent 不应该只是一个“把 prompt 发给模型”的 helper。它需要有独立的：

- system prompt
- message history
- context 构建
- tool pool
- permission mode
- transcript
- abort / cancel 状态
- 同步或后台运行生命周期

但 BigCode v1 不需要完整照搬 Claude Code 的所有复杂能力。优先复现核心业务，复杂分支可以后置：

- Agent Swarms / teammate / named agent / SendMessage 路由：后置。
- remote CCR：后置。
- MCP agent-specific servers：后置。
- 用户自定义 subAgent hooks、agent frontmatter hooks：后置。内置 `SubagentStart` / `SubagentStop` 生命周期事件属于 v1，用于 sidechain transcript、状态记录和父子上下文同步。
- Skill preload 和 agent memory snapshot：后置；但 v1 需要继承父 agent 已启用的 Skill / MCP capability index，并按权限模式收窄可用工具。
- fork subagent prompt-cache 继承：后置。
- worktree isolation：后置，v1 可以只保留接口字段。
- 前台 agent 运行中转后台：后置。
- SDK progress summaries、handoff classifier、Perfetto、复杂 TUI 分组：后置。

核心原则：

- subAgent 是父 agent 通过工具启动的独立 agent loop，不直接污染父 agent 的 `messages`。
- 父 agent 只看到 AgentTool 的工具结果，必要时可通过 transcript/task output 查看细节。
- subAgent 和主 agent 共用 Context 系统的 `messages -> context_messages -> api_messages` 构建链路。
- subAgent 的 tool_result 仍然由 Context 系统统一映射和归一化。
- 默认隔离可变状态，只有明确需要的只读配置和工具定义从父上下文继承。
- v1 先保证同步 subAgent 正确，再加后台 task。

## 对应 Claude Code 源码

当前源码里和 subAgent 相关的核心文件：

- `/home/qt/claude-code-rev/src/tools/AgentTool/AgentTool.tsx`：Agent 工具入口，负责输入 schema、agent 选择、同步/后台运行、任务注册和结果返回。
- `/home/qt/claude-code-rev/src/tools/AgentTool/runAgent.ts`：真正运行 subagent，构造 agent prompt、tools、context，并驱动 `query()`。
- `/home/qt/claude-code-rev/src/utils/forkedAgent.ts`：`createSubagentContext()`，体现“默认隔离、按需共享”的上下文原则。
- `/home/qt/claude-code-rev/src/tools/AgentTool/loadAgentsDir.ts`：AgentDefinition 类型、内置/自定义 agent 加载、frontmatter 解析。
- `/home/qt/claude-code-rev/src/tools/AgentTool/agentToolUtils.ts`：工具过滤、结果汇总、后台生命周期、进度统计。
- `/home/qt/claude-code-rev/src/tasks/LocalAgentTask/`：后台 agent task 状态、进度、完成/失败/取消。

BigCode v1 参考这些文件的核心行为，但做 Python 化和简化。

## 推荐目录结构

```txt
bigcode/
  subagents/
    __init__.py
    definitions.py      # AgentDefinition、AgentTool 输入输出、运行结果类型
    builtins.py         # 内置 agents，例如 general-purpose / explorer / code-reviewer / planAgent
    loader.py           # markdown/json agent 加载和 frontmatter 解析
    tool.py             # AgentTool 实现，父 agent 调用入口
    runner.py           # run_subagent 主流程
    context.py          # create_subagent_context，隔离父 ToolExecutionContext
    tasks.py            # 后台 AgentTaskState、任务注册、查询、取消
    transcript.py       # sidechain transcript 写入和读取
    result.py           # finalize_agent_result、统计、结果提取
```

与现有系统的关系：

- `context/`：负责 subAgent 每一轮模型请求的上下文构建。
- `tools/`：提供可执行工具定义和工具权限。
- `hooks/`：`run_subagent()` 启停时触发 `SubagentStart` / `SubagentStop`；v1 不允许用户配置 subAgent 专属 hooks。
- `agent_loop.py` 或主 loop：被 subAgent 复用，不要写一套完全不同的循环。
- `transcript.py`：主 transcript 和 subAgent sidechain transcript 可以共用底层 JSONL writer。

## 核心数据流

```txt
父 agent assistant message:
  tool_use: Agent({description, prompt, subagent_type, ...})

AgentTool.call:
  1. 校验输入
  2. 解析 selected_agent
  3. 解析模型、权限、工具池
  4. 构造 prompt_messages = [UserMessage(prompt)]
  5. 同步运行或注册后台任务

run_subagent:
  1. 创建 agent_id
  2. 构造 SubAgentContext
  3. 触发 HookBus.emit(SubagentStart)
  4. 构造 agent system prompt
  5. 写入 sidechain 初始 transcript
  6. 调用通用 agent loop
  7. 记录 assistant/user/progress 到 sidechain transcript
  8. 汇总最后 assistant 文本和使用量
  9. 触发 HookBus.emit(SubagentStop)

父 agent 下一轮:
  AgentTool 的返回值被 Tool 系统转为 tool_result
  Context 系统把 tool_result 纳入父 agent api_messages
```

## AgentDefinition

AgentDefinition 是 subAgent 的配置单元，对应 Claude Code 的 `AgentDefinition`。

v1 推荐只保留核心字段：

```py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


AgentSource = Literal["built-in", "user", "project", "managed"]
PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    system_prompt: str
    source: AgentSource = "project"
    tools: list[str] | None = None
    disallowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    permission_mode: PermissionMode | None = None
    max_turns: int | None = None
    background: bool = False
    color: str | None = None
    filename: str | None = None
    base_dir: Path | None = None
```

字段语义：

- `name`：模型调用 AgentTool 时传入的 `subagent_type`。
- `description`：注入 AgentTool 描述，告诉父 agent 什么时候使用该子代理。
- `system_prompt`：subAgent 的核心行为指令。
- `tools`：工具 allowlist；`None` 或 `["*"]` 表示允许所有经过 subAgent 过滤后的工具。
- `disallowed_tools`：额外 denylist。
- `model`：agent 默认模型；必须引用 `Config.md` 的模型注册表，`model: inherit` 解析为继承父模型；调用时传入 `model` 可覆盖。
- `permission_mode`：agent 默认权限模式。
- `max_turns`：最多 agentic turn 数，防止循环调用工具。
- `background`：该 agent 默认后台运行。

## 内置 Agent 种类设计

BigCode v1 不需要设计很多 agent。最核心、最常用的 4 个就够：

| Agent | 定位 | 是否可写 | 典型使用时机 |
|-------|------|----------|--------------|
| `general-purpose` | 通用任务执行 | 可写，取决于权限模式 | 父 agent 想委派一段完整、多步骤工作 |
| `explorer` | 只读代码探索 | 不可写 | 需要快速理解陌生模块、调用链、错误来源 |
| `code-reviewer` | 只读代码审查 | 不可写 | 改完代码后找 bug、回归、遗漏测试 |
| `planAgent` | 实施计划设计 | 不可写 | 需要把已知事实整理成可执行计划 |

默认调度规则：

- `subagent_type` 为空时使用 `general-purpose`。
- “先了解代码再决定怎么改”优先用 `explorer`。
- “已经改完，请检查风险”优先用 `code-reviewer`。
- “需要落地方案/拆解步骤/明确验收标准”优先用 `planAgent`。
- 不要把一个任务拆给多个 agent，除非子问题彼此独立。
- 父 agent 必须在 prompt 中交代背景、文件路径、已知事实和期望输出；普通 subAgent v1 不继承父对话全文。

### 1. general-purpose

用途：

- 默认通用 agent。
- 适合中等复杂度、多步骤、可独立完成的任务。
- 可以读代码、搜索、运行命令、按权限编辑文件。

不适合：

- 只需要回答一个很小的问题。
- 父 agent 还没理解任务，只想把“理解问题”外包。
- 需要严格只读审查时，应使用 `explorer` 或 `code-reviewer`。

工具策略：

- `tools=None`，表示使用 subAgent 默认过滤后的全部工具。
- 权限模式默认继承父 agent。
- `max_turns=10`，避免长时间失控。

输出契约：

- 最终回复给父 agent，而不是直接面向用户。
- 说明完成了什么、改了哪些文件、还剩什么风险。
- 如果修改了代码，必须说明验证方式和结果。

```py
GENERAL_PURPOSE_AGENT = AgentDefinition(
    name="general-purpose",
    description=(
        "General-purpose agent for independent multi-step coding tasks, "
        "research, implementation, and debugging."
    ),
    system_prompt=(
        "You are a general-purpose BigCode subagent. Complete the delegated "
        "task autonomously. Read the relevant code before making claims. "
        "Make small, auditable changes when editing is allowed. In your final "
        "response, summarize what you did, files changed, validation run, "
        "and any remaining risks. Be concise because the parent agent will "
        "use your result to continue the conversation."
    ),
    source="built-in",
    tools=None,
    max_turns=10,
)
```

### 2. explorer

用途：

- 只读探索 agent。
- 适合理解陌生模块、查找入口、梳理调用链、定位可能的 bug 来源。
- 用来把“代码搜索和阅读噪音”留在子上下文里。

不适合：

- 直接修改代码。
- 长篇泛泛总结整个仓库。
- 替父 agent 做最终实现决策；它只给事实和候选方向。

工具策略：

- 只允许只读工具和安全命令。
- `permission_mode="plan"`，禁止编辑。
- `max_turns=6`。
- Bash 仅用于只读命令，例如 `pwd`、`ls`、`rg`、`sed`、`git status`、`git diff --stat`、语言自带只读 introspection；具体危险命令由 Tool 权限层拦截。

输出契约：

- 用短列表报告发现。
- 必须给出关键文件路径/符号名。
- 区分“确定事实”和“推测”。
- 不提出大规模重构建议，除非 prompt 明确要求。

```py
EXPLORER_AGENT = AgentDefinition(
    name="explorer",
    description=(
        "Read-only code exploration agent for finding files, tracing behavior, "
        "and reporting concrete implementation facts."
    ),
    system_prompt=(
        "You are a read-only exploration subagent. Do not modify files. "
        "Inspect the codebase, trace the relevant flow, and report concrete "
        "facts with file paths and symbol names. Separate confirmed facts from "
        "inferences. Keep the final response focused and useful for the parent "
        "agent's next implementation step."
    ),
    source="built-in",
    tools=["Read", "Grep", "Glob", "Bash"],
    disallowed_tools=["Edit", "Write", "MultiEdit", "NotebookEdit", "Agent"],
    permission_mode="plan",
    max_turns=6,
)
```

### 3. code-reviewer

用途：

- 只读审查 agent。
- 适合在父 agent 完成实现后进行第二视角检查。
- 重点找 bug、行为回归、安全问题、边界遗漏、测试缺口。

不适合：

- 代替测试运行。
- 代替实现。
- 做风格化、吹毛求疵式重构建议。

工具策略：

- 只读工具。
- 可运行只读检查命令，例如 `git diff`、`rg`、`sed`、轻量测试 dry run。
- 默认 `permission_mode="plan"`。
- `max_turns=7`。

输出契约：

- 按严重程度排序。
- 每条问题必须引用文件路径和具体原因。
- 如果没有发现问题，要明确说“未发现阻塞问题”，并列出仍未验证的风险。
- 不要输出长篇总结；父 agent 需要的是可行动审查结果。

```py
CODE_REVIEWER_AGENT = AgentDefinition(
    name="code-reviewer",
    description=(
        "Read-only review agent for bugs, regressions, security risks, "
        "and missing tests after code changes."
    ),
    system_prompt=(
        "You are a code review subagent. Review the provided change or target "
        "area for concrete bugs, regressions, security issues, and missing "
        "tests. Do not modify files. Lead with findings ordered by severity. "
        "Each finding must include a file path and explain the impact. If no "
        "issues are found, say so clearly and mention remaining test gaps."
    ),
    source="built-in",
    tools=["Read", "Grep", "Glob", "Bash"],
    disallowed_tools=["Edit", "Write", "MultiEdit", "NotebookEdit", "Agent"],
    permission_mode="plan",
    max_turns=7,
)
```

### 4. planAgent

用途：

- 只读计划 agent。
- 适合把探索结果、用户目标和约束整理成可执行实现计划。
- 适合在进入实现前明确改动范围、接口、数据流、边界情况和验收标准。

不适合：

- 直接修改代码。
- 替代 `explorer` 做大范围事实调查。
- 写空泛路线图；它必须输出可交给实现者执行的具体计划。

工具策略：

- 只允许只读工具。
- 可以读取文件、搜索代码、查看 git diff 或配置，用来确认计划依据。
- 不运行会产生副作用的命令。
- `permission_mode="plan"`。
- `max_turns=6`。

输出契约：

- 输出决策完整的实施计划。
- 必须包含目标、关键改动、接口/数据流影响、测试方案和明确假设。
- 文件路径只列关键位置，不要做冗长文件清单。
- 不问“是否继续”，父 agent 会决定何时执行。

```py
PLAN_AGENT = AgentDefinition(
    name="planAgent",
    description=(
        "Read-only planning agent for turning known facts and constraints into "
        "a concrete, executable implementation plan."
    ),
    system_prompt=(
        "You are a read-only planning subagent. Do not modify files. Use the "
        "provided context and any necessary read-only inspection to produce a "
        "decision-complete implementation plan. Include the goal, key changes, "
        "interfaces or data flow affected, tests and acceptance criteria, and "
        "explicit assumptions. Keep the plan concise and directly executable "
        "by the parent agent or another implementer."
    ),
    source="built-in",
    tools=["Read", "Grep", "Glob", "Bash"],
    disallowed_tools=["Edit", "Write", "MultiEdit", "NotebookEdit", "Agent"],
    permission_mode="plan",
    max_turns=6,
)
```

内置 agent 注册：

```py
def get_builtin_agents() -> list[AgentDefinition]:
    return [
        GENERAL_PURPOSE_AGENT,
        EXPLORER_AGENT,
        CODE_REVIEWER_AGENT,
        PLAN_AGENT,
    ]
```

后续如果要扩展，不建议一开始增加很多细碎 agent。优先观察真实任务中是否反复出现稳定模式，再新增：

- `planner`：只负责输出实施计划，不执行。
- `debugger`：专门定位运行时错误。
- `doc-writer`：专门写 README / 文档。
- `security-reviewer`：专门审查安全风险。

## Agent 加载

BigCode v1 支持两类 agent：

- 内置 agent：代码中定义。
- Markdown agent：用户或项目目录下定义。

推荐发现路径：

```txt
~/.bigcode/agents/*.md
<repo>/.bigcode/agents/*.md
<cwd>/.bigcode/agents/*.md
```

Markdown 格式：

```md
---
name: code-reviewer
description: Review code changes for bugs, risks, and missing tests.
tools: Read, Grep, Glob, Bash
model: inherit
permission_mode: plan
max_turns: 8
background: false
---

You are a code review subagent. Focus on concrete bugs, regressions,
security risks, and missing tests. Do not modify files.
```

解析规则：

- `name` 和 `description` 必填。
- markdown body 是 `system_prompt`。
- `tools` 支持字符串或列表；逗号分隔后 trim。
- `model: inherit` 视为 `None`，表示继承父 agent 的主模型。
- `permission_mode` 不合法时忽略并记录 warning。
- `max_turns` 必须是正整数。
- 不认识的 frontmatter 字段先忽略，不阻塞加载。

```py
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentDefinitionsResult:
    active_agents: list[AgentDefinition]
    all_agents: list[AgentDefinition]
    failed_files: list[tuple[Path, str]] = field(default_factory=list)


async def load_agent_definitions(cwd: Path) -> AgentDefinitionsResult:
    builtins = get_builtin_agents()
    custom, failed = await load_markdown_agents(cwd)

    # 后加载的同名 agent 覆盖先加载的 agent。
    # 优先级：built-in < user < project < cwd-local。
    merged: dict[str, AgentDefinition] = {}
    for agent in [*builtins, *custom]:
        merged[agent.name] = agent

    return AgentDefinitionsResult(
        active_agents=list(merged.values()),
        all_agents=[*builtins, *custom],
        failed_files=failed,
    )
```

## AgentTool 接口

AgentTool 是父 agent 唯一看到的 subAgent 入口。

从系统边界看，`AgentTool` 是 Tool 系统中的一个工具：它接受模型的 tool_use、继承父级权限和 workspace，并最终返回 `ToolRunResult`。本节定义 `AgentTool` 内部如何选择 agent、构造 `SubAgentContext`、运行子 Agent 和汇总结果；权限判定、路径安全和执行侧输出上限仍由 `Tool.md` 的 Tool Runner 统一负责。

输入 schema：

```py
from pydantic import BaseModel, Field


class AgentToolInput(BaseModel):
    description: str = Field(..., description="Short 3-5 word task description.")
    prompt: str = Field(..., description="Full task prompt for the subagent.")
    subagent_type: str | None = Field(None, description="Agent type to use.")
    model: str | None = Field(None, description="Optional model override.")
    run_in_background: bool | None = Field(False, description="Run as background task.")
```

输出 schema：

```py
class AgentCompletedOutput(BaseModel):
    status: Literal["completed"] = "completed"
    agent_id: str
    agent_type: str
    content: str
    total_tool_use_count: int
    total_duration_ms: int
    total_tokens: int


class AgentAsyncLaunchedOutput(BaseModel):
    status: Literal["async_launched"] = "async_launched"
    agent_id: str
    agent_type: str
    description: str
    prompt: str
    output_file: str


AgentToolOutput = AgentCompletedOutput | AgentAsyncLaunchedOutput
```

调用入口：

```py
async def call_agent_tool(
    tool_input: AgentToolInput,
    parent_ctx: "ToolExecutionContext",
) -> AgentToolOutput:
    agent_defs = parent_ctx.agent_definitions.active_agents
    selected = select_agent_definition(tool_input.subagent_type, agent_defs)

    should_run_background = bool(tool_input.run_in_background or selected.background)
    params = build_run_subagent_params(tool_input, selected, parent_ctx)

    if should_run_background:
        task = register_agent_task(params)
        start_background_subagent(task, params)
        return AgentAsyncLaunchedOutput(
            agent_id=task.agent_id,
            agent_type=selected.name,
            description=tool_input.description,
            prompt=tool_input.prompt,
            output_file=task.output_file,
        )

    result = await run_subagent(params)
    return AgentCompletedOutput(
        agent_id=result.agent_id,
        agent_type=result.agent_type,
        content=result.content,
        total_tool_use_count=result.total_tool_use_count,
        total_duration_ms=result.total_duration_ms,
        total_tokens=result.total_tokens,
    )
```

选择规则：

- `subagent_type` 为空时使用 `general-purpose`。
- 找不到对应 agent 时抛出清晰错误，列出可用 agent。
- 如果 agent 被权限规则禁用，错误应说明是权限禁用，不要伪装成找不到。
- `model` 调用参数优先级高于 agent definition。

## SubAgentContext 隔离

Claude Code 的关键点是：subAgent 默认隔离可变状态，避免破坏父 agent。

Python v1 推荐：

```py
from dataclasses import dataclass, field


@dataclass
class SubAgentContext:
    agent_id: str
    agent_type: str
    messages: list["MessageBase"]
    options: "AgentLoopOptions"
    permission_mode: PermissionMode
    read_file_state: "ReadFileState"
    abort_controller: "AbortController"
    hook_bus: "HookBus | None" = None
    session_id: str | None = None
    parent_session_id: str | None = None
    sidechain_transcript_path: str | None = None
    should_avoid_permission_prompts: bool = False
    local_denial_count: int = 0
    metadata: dict[str, object] = field(default_factory=dict)
```

创建规则：

```py
def create_subagent_context(
    parent_ctx: "ToolExecutionContext",
    *,
    agent_id: str,
    agent_type: str,
    messages: list["MessageBase"],
    options: "AgentLoopOptions",
    permission_mode: PermissionMode,
    is_background: bool,
) -> SubAgentContext:
    return SubAgentContext(
        agent_id=agent_id,
        agent_type=agent_type,
        messages=list(messages),
        options=options,
        permission_mode=permission_mode,
        read_file_state=parent_ctx.read_file_state.clone(),
        abort_controller=(
            AbortController()
            if is_background
            else parent_ctx.abort_controller.create_child()
        ),
        hook_bus=parent_ctx.hook_bus,
        session_id=f"{parent_ctx.session_id}:agent:{agent_id}",
        parent_session_id=parent_ctx.session_id,
        sidechain_transcript_path=get_subagent_transcript_path(
            parent_ctx.session_id,
            agent_id,
        ),
        should_avoid_permission_prompts=is_background,
    )
```

隔离规则：

- `messages`：subAgent 自己维护，父 `messages` 不追加子 agent 内部对话。
- `read_file_state`：clone 父状态，避免读文件预算和缓存互相污染；如果 subAgent 写入文件，写入成功后必须把目标文件快照同步回父级 `ReadFileState`。
- `abort_controller`：同步 agent 可跟随父取消；后台 agent 使用独立 controller。
- `permission_mode`：父模式为 `bypassPermissions` / `acceptEdits` 时可优先；否则 agent 定义可以覆盖。
- UI callbacks：v1 默认不共享。
- task registry：后台任务需要能写入全局 task store。

## 工具池与权限

subAgent 工具池不能简单复用父 agent 当前 tool restrictions。Claude Code 的核心逻辑是：worker 工具池由 agent 自己的权限模式和工具 allowlist 决定。

v1 推荐流程：

```txt
父上下文可用工具
  -> 基础 subAgent 过滤
  -> MCP / Skill 外部能力收窄
  -> agent.tools allowlist
  -> agent.disallowed_tools denylist
  -> 后台 agent 过滤交互型工具
  -> resolved_tools
```

默认过滤：

- 禁用会直接控制父 UI 的工具。
- 后台 agent 禁用需要交互确认的工具，除非权限模式允许自动处理。
- 自定义 agent 默认不能启动新的 AgentTool，避免递归失控。
- `Read`、`Grep`、`Glob`、安全 `Bash`、编辑工具是否开放由 permission mode 决定。
- `explorer` / `code-reviewer` / `planAgent` 只允许 `SkillLoad`、`SkillResourceRead`、`ExternalPromptGet`、`ExternalResourceRead` 和明确 read-only 的 MCP-backed 普通工具。
- `general-purpose` 可以使用父 agent 已发现且权限允许的 MCP-backed 普通工具，但不能新增 MCP server、修改 MCP 配置或扩大 Skill 注册目录。
- 后台 subAgent 没有显式 allow 时，不得触发 MCP stdio connect、HTTP connect 或其他需要交互确认的外部调用。

```py
def resolve_agent_tools(
    agent: AgentDefinition,
    available_tools: list["BaseTool"],
    *,
    is_background: bool,
) -> list["BaseTool"]:
    tools = filter_base_tools_for_subagent(available_tools, is_background=is_background)

    if agent.disallowed_tools:
        denied = {parse_tool_name(t) for t in agent.disallowed_tools}
        tools = [tool for tool in tools if tool.name not in denied]

    if agent.tools is None or agent.tools == ["*"]:
        return tools

    allowed = {parse_tool_name(t) for t in agent.tools}
    return [tool for tool in tools if tool.name in allowed]
```

权限模式优先级：

```txt
父 `bypassPermissions` / `acceptEdits`
  > AgentTool 调用显式 mode（v1 可无）
  > AgentDefinition.permission_mode
  > 父 permission_mode
```

`ask` 是权限决策结果，不是 `PermissionMode`。如果 BigCode v1 暂时没有完整权限系统，也要保留字段和传递链路，避免以后重构 AgentDefinition。

## RunSubAgentParams

```py
@dataclass
class RunSubAgentParams:
    agent_definition: AgentDefinition
    prompt_messages: list["MessageBase"]
    parent_ctx: "ToolExecutionContext"
    selected_model: str
    is_background: bool
    description: str
    max_turns: int | None = None
    agent_id: str | None = None
```

构造 prompt messages：

```py
def build_prompt_messages(prompt: str) -> list["MessageBase"]:
    return [UserMessage(prompt)]
```

v1 不做 fork context，所以普通 subAgent 从零上下文开始。父 agent 必须在 `prompt` 里写清楚任务背景、文件路径、已知事实和期望输出。

## subAgent System Prompt

subAgent 的 system prompt 由 agent 定义和环境动态信息组成：

```py
async def build_subagent_system_prompt(
    agent: AgentDefinition,
    ctx: SubAgentContext,
) -> str:
    parts = [
        agent.system_prompt,
        "You are running as a BigCode subagent.",
        "Complete only the delegated task.",
        "Return a concise final response for the parent agent.",
        f"Agent type: {agent.name}",
        f"Permission mode: {ctx.permission_mode}",
    ]
    return "\n\n".join(part.strip() for part in parts if part.strip())
```

如果要更贴近 Claude Code，可以调用 Context 系统已有的 `build_system_prompt()`，但传入 subAgent 自己的 deps：

- `cwd` 继承父 cwd。
- `tools` 使用 resolved subAgent tools。
- `permission_mode` 使用 resolved permission mode。
- `instruction_context` 可以复用项目 BigCode instructions。
- `mainLoopModel` 替换为 subAgent selected model。

## run_subagent 主流程

```py
import time


async def run_subagent(params: RunSubAgentParams) -> "AgentRunResult":
    start = time.monotonic()
    agent = params.agent_definition
    agent_id = params.agent_id or new_agent_id()

    resolved_tools = resolve_agent_tools(
        agent,
        params.parent_ctx.available_tools,
        is_background=params.is_background,
    )
    permission_mode = resolve_permission_mode(agent, params.parent_ctx)
    options = build_agent_loop_options(
        parent_options=params.parent_ctx.options,
        tools=resolved_tools,
        model=params.selected_model,
        is_background=params.is_background,
    )

    sub_ctx = create_subagent_context(
        params.parent_ctx,
        agent_id=agent_id,
        agent_type=agent.name,
        messages=params.prompt_messages,
        options=options,
        permission_mode=permission_mode,
        is_background=params.is_background,
    )

    system_prompt = await build_subagent_system_prompt(agent, sub_ctx)
    if sub_ctx.hook_bus:
        await sub_ctx.hook_bus.emit(
            "SubagentStart",
            HookInput(
                hook_event_name="SubagentStart",
                session_id=sub_ctx.session_id,
                cwd=str(sub_ctx.cwd),
                permission_mode=permission_mode,
                agent_id=agent_id,
                payload={
                    "agent_name": agent.name,
                    "sidechain_path": sub_ctx.sidechain_transcript_path,
                },
            ),
        )
    await record_subagent_transcript(agent_id, params.prompt_messages)

    output_messages: list[MessageBase] = []

    try:
        async for message in run_agent_loop(
            messages=params.prompt_messages,
            system_prompt=system_prompt,
            tool_ctx=sub_ctx,
            max_turns=params.max_turns or agent.max_turns,
        ):
            output_messages.append(message)
            if is_recordable_subagent_message(message):
                await record_subagent_transcript(agent_id, [message])

        result = finalize_agent_result(
            agent_id=agent_id,
            agent_type=agent.name,
            messages=output_messages,
            start_time=start,
            selected_model=params.selected_model,
        )
        if sub_ctx.hook_bus:
            await sub_ctx.hook_bus.emit(
                "SubagentStop",
                HookInput(
                    hook_event_name="SubagentStop",
                    session_id=sub_ctx.session_id,
                    cwd=str(sub_ctx.cwd),
                    permission_mode=permission_mode,
                    agent_id=agent_id,
                    payload={
                        "agent_name": agent.name,
                        "sidechain_path": sub_ctx.sidechain_transcript_path,
                        "result_summary": result.content,
                    },
                ),
            )
        return result
    finally:
        sub_ctx.read_file_state.clear()
        await cleanup_subagent_runtime(sub_ctx)
```

`run_agent_loop()` 应该尽量复用主 agent loop，而不是写独立版本。区别通过参数体现：

- messages 是 subAgent 自己的 messages。
- tool_ctx 是 SubAgentContext。
- system_prompt 是 subAgent prompt。
- tools 是 subAgent resolved tools。
- max_turns 可限制。

## AgentRunResult

```py
@dataclass
class AgentRunResult:
    agent_id: str
    agent_type: str
    content: str
    messages: list["MessageBase"]
    total_tool_use_count: int
    total_duration_ms: int
    total_tokens: int
    usage: dict[str, object] = field(default_factory=dict)
```

结果汇总规则：

- 找最后一条 assistant message。
- 提取其中的 text block。
- 如果最后 assistant 只有 tool_use，向前找最近一条有 text 的 assistant。
- 没有 assistant message 时抛出 `SubAgentNoResponseError`。
- 统计所有 assistant message 中的 tool_use 数量。
- token 统计优先使用最后 assistant usage；没有 usage 时用粗估。

```py
def finalize_agent_result(
    *,
    agent_id: str,
    agent_type: str,
    messages: list["MessageBase"],
    start_time: float,
    selected_model: str,
) -> AgentRunResult:
    assistant = find_last_assistant_with_text(messages)
    if assistant is None:
        raise SubAgentNoResponseError(f"Subagent {agent_type} produced no assistant response.")

    content = extract_text_content(assistant)
    duration_ms = int((time.monotonic() - start_time) * 1000)
    tool_count = count_tool_uses(messages)
    usage = getattr(assistant, "usage", {}) or {}

    return AgentRunResult(
        agent_id=agent_id,
        agent_type=agent_type,
        content=content or "[Subagent completed with no text output]",
        messages=messages,
        total_tool_use_count=tool_count,
        total_duration_ms=duration_ms,
        total_tokens=estimate_usage_tokens(usage, messages),
        usage=usage,
    )
```

父 agent tool_result 内容建议渲染为：

```txt
Subagent <agent_type> completed.

<final content>

Stats:
- agent_id: <id>
- tool uses: <n>
- duration: <ms>
```

如果父 agent 只需要机器可读结果，可以把 `AgentCompletedOutput` 作为结构化 tool result data，再由 Context 映射成文本或 JSON。

## 后台 Agent Task

v1 后台能力要简单：启动后立即返回 `async_launched`，后台完成后写 task output 和 transcript。

```py
TaskStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


@dataclass
class AgentTaskState:
    agent_id: str
    agent_type: str
    description: str
    prompt: str
    status: TaskStatus = "queued"
    output_file: str = ""
    result: AgentRunResult | None = None
    error: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    total_tool_use_count: int = 0
    total_tokens: int = 0
```

任务注册：

```py
def register_agent_task(params: RunSubAgentParams) -> AgentTaskState:
    agent_id = params.agent_id or new_agent_id()
    task = AgentTaskState(
        agent_id=agent_id,
        agent_type=params.agent_definition.name,
        description=params.description,
        prompt=extract_first_user_text(params.prompt_messages),
        output_file=get_agent_task_output_path(agent_id),
    )
    TASK_STORE[agent_id] = task
    return task
```

后台执行：

```py
def start_background_subagent(task: AgentTaskState, params: RunSubAgentParams) -> None:
    async def _run() -> None:
        task.status = "running"
        task.started_at = time.monotonic()
        try:
            result = await run_subagent(params)
            task.result = result
            task.status = "completed"
            task.total_tool_use_count = result.total_tool_use_count
            task.total_tokens = result.total_tokens
            await write_task_output(task.output_file, render_agent_result(result))
        except asyncio.CancelledError:
            task.status = "cancelled"
            await write_task_output(task.output_file, "[Subagent cancelled]")
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            await write_task_output(task.output_file, f"[Subagent failed]\n{exc}")
        finally:
            task.completed_at = time.monotonic()

    create_background_task(_run())
```

后续可以增加 `TaskOutput` 工具，让父 agent 或用户读取后台结果。v1 不需要复杂 progress summary，只要输出文件和 task 状态可查。

## Transcript

subAgent transcript 是 sidechain，不和父 transcript 混在一起。

推荐路径：

```txt
~/.bigcode/projects/<project-id>/subagents/<agent-id>.jsonl
```

记录内容：

```json
{"type":"user","uuid":"...","parent_uuid":null,"content":"delegated prompt"}
{"type":"assistant","uuid":"...","content":[{"type":"text","text":"..."}]}
{"type":"user","uuid":"...","content":[{"type":"tool_result","tool_use_id":"...","content":"..."}]}
```

规则：

- `run_subagent()` 开始时写入初始 prompt messages。
- agent loop 每产出 recordable message 后追加。
- `progress` 可以写 sidechain transcript，但不进入 API。
- 父 transcript 不写 subAgent 内部 messages。
- 父 transcript 只写 AgentTool 的 tool_use 和最终 tool_result。
- resume 后台任务时，可通过 sidechain transcript 重建 subAgent messages。

```py
def is_recordable_subagent_message(msg: MessageBase) -> bool:
    return msg.type in {"user", "assistant", "progress"} or (
        msg.type == "system" and getattr(msg, "subtype", None) == "compact_boundary"
    )
```

## 与 Context 系统的关系

subAgent 不直接拼 API messages。它调用 Context 系统：

```txt
SubAgentContext.messages
  -> context.build_context_for_api(...)
  -> model.complete(...)
  -> assistant message
  -> tool runner
  -> ToolRunResult
  -> Context.tool_run_result_to_message(...)
  -> SubAgentContext.messages
```

需要注意：

- subAgent 的 `messages` 是 Context 的事实来源，但只在 subAgent 内部有效。
- subAgent 的 attachments、capability index、compact state 应该独立。
- subAgent 可以继承父 agent 当前已发现的 MCP / Skill capability 摘要，但不能自行新增 MCP server 或扩大 Skill 注册目录。
- subAgent 的 `progress` 不进入 API。
- subAgent 的 tool_result pairing 仍由 Context normalizer 兜底。
- 父 agent 不应该把 subAgent 的完整中间工具噪音读进上下文，除非用户明确要求查看。

## 错误处理

常见错误：

```py
class SubAgentError(Exception):
    pass


class AgentTypeNotFoundError(SubAgentError):
    pass


class SubAgentNoResponseError(SubAgentError):
    pass


class SubAgentMaxTurnsError(SubAgentError):
    pass
```

处理规则：

- agent type 找不到：同步抛错，作为 AgentTool error tool_result 返回父 agent。
- subAgent 没有 assistant 输出：同步抛错；后台 task 标记 failed。
- 达到 max_turns：把已有最后文本作为 partial result；没有文本则 failed。
- 用户取消同步 subAgent：传播父 abort。
- 用户取消后台 subAgent：标记 task cancelled，尽量保留 partial result。
- 工具权限拒绝：作为 subAgent 内部 tool_result，让 subAgent 自己处理；不要直接杀死 subAgent。

## v1 省略清单

明确不在第一版实现：

- `name` / `team_name` / teammate panel / SendMessage。
- fork 模式：省略 `subagent_type` 时不是 fork，而是 `general-purpose`。
- prompt cache 共享。
- remote agent。
- worktree 自动创建和清理。
- agent frontmatter hooks。
- 用户配置的 subAgent 专属 hooks。
- agent MCP server。
- 启动时自动 preload Skill 全文。
- agent memory。
- 后台 progress summary。
- handoff classifier。
- 复杂 UI 渲染。

可以保留字段但不实现行为：

- `color`
- `background`

以下字段 v1 必须实现最小行为，因为前面的内置 agent 和运行流程已经依赖它们：

- `permission_mode`：用于只读 agent 和权限收窄。
- `max_turns`：用于防止子 Agent 工具循环失控。
- `model`：支持 agent definition 默认模型和 `AgentTool` 调用覆盖。

这些字段对配置兼容和后续扩展有价值。

## 测试建议

单测：

- markdown agent 缺少 `name` 时跳过。
- markdown agent 缺少 `description` 时记录 parse error。
- `tools` 字符串能解析为列表。
- `model: inherit` 解析为 `None`。
- 同名 agent 按优先级覆盖。
- `subagent_type=None` 默认选择 `general-purpose`。
- 找不到 `subagent_type` 返回包含可用 agent 列表的错误。
- `resolve_agent_tools()` 正确处理 allowlist。
- `resolve_agent_tools()` 正确处理 disallowed tools。
- 后台 agent 过滤交互型工具。
- `create_subagent_context()` clone 父 read_file_state。
- subAgent messages 追加不影响父 messages。
- 同步 subAgent 成功时返回最后 assistant text。
- 最后一条 assistant 是 tool_use 时，向前找最近 text。
- 没有 assistant 时抛 `SubAgentNoResponseError`。
- `max_turns` 能终止循环。
- sidechain transcript 会记录初始 prompt 和 assistant。
- `progress` 写 transcript 但不进入 API。
- 后台任务 completed 会写 output_file。
- 后台任务 failed 会写错误。
- cancel 后台任务会标记 cancelled。

集成 smoke：

- 父 agent 调用 `AgentTool(general-purpose)`，subAgent 调用只读工具后返回摘要。
- 父 agent 收到合法 tool_result，并继续下一轮模型请求。
- subAgent 进行一次工具调用后，Context 正确配对 tool_use/tool_result。
- subAgent 超过 `max_turns` 不会无限循环。
- 两个后台 subAgent 并发运行时，task 状态和 transcript 互不污染。
- resume 后能读取 sidechain transcript 或 task output。

## v1 实现顺序

1. 实现 `definitions.py` 的 dataclass / pydantic 类型。
2. 实现 `builtins.py`，至少提供 `general-purpose`。
3. 实现 `loader.py`，支持 markdown agent。
4. 实现 `context.py`，完成 `create_subagent_context()`。
5. 实现 `result.py`，完成 `finalize_agent_result()`。
6. 实现 `runner.py`，复用主 agent loop 跑同步 subAgent。
7. 实现 `tool.py`，接入 AgentTool 调用入口。
8. 接入 Context 系统，保证 subAgent messages 正常构建 API 请求。
9. 加入 sidechain transcript。
10. 加入最小后台 task。
11. 加入 task output 查询和取消。
12. 补齐单测和集成 smoke。

这样 BigCode 可以先获得 Claude Code subAgent 的核心形态：父 agent 委派任务、子 agent 独立运行、工具和上下文隔离、结果汇总回父 agent、后台任务可追踪，同时避免一开始就陷入 Claude Code 的复杂实验分支。
