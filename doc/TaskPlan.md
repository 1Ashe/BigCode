# Task 与 Plan Mode 系统设计（Python 版）

## 总纲

BigCode 的 Task/Plan 目标是复现 Claude Code 里两条核心业务线：

1. **Task List**：在执行阶段用结构化任务清单跟踪多步骤工作。
2. **Plan Mode**：在实现前进入只读规划状态，探索代码、和用户澄清意图，并把最终计划写成 markdown 文件供审批。

这两个概念要保持边界清楚：

- Task List 是执行管理能力，不是 Plan Mode 的必需产物。
- Plan Mode 的最终产物是 plan markdown 文件，而不是 task list。
- Plan Mode 下可以读代码、搜索、提问、编辑 plan 文件；不能修改 workspace 或运行非只读命令。
- 用户批准 plan 以后，才进入实现阶段；此时可以根据 approved plan 使用 Task 工具拆分和跟踪执行。

当前文档不设计 Background Task。后台 subAgent、shell task、输出文件、取消和通知等生命周期能力先由 `subAgent.md` 保留接口或后续单独设计，避免 Task/Plan v1 过重。

## 对应 Claude Code 源码

Task List 重点参考：

- `/home/qt/claude-code-rev/src/utils/tasks.ts`：任务 JSON 存储、ID 分配、锁、依赖、claim、list/update/delete。
- `/home/qt/claude-code-rev/src/tools/TaskCreateTool/`、`/home/qt/claude-code-rev/src/tools/TaskUpdateTool/`、`/home/qt/claude-code-rev/src/tools/TaskListTool/`、`/home/qt/claude-code-rev/src/tools/TaskGetTool/`：模型可调用的任务工具。
- `/home/qt/claude-code-rev/src/hooks/useTasksV2.ts`：任务列表 watcher、隐藏 completed 任务、UI 刷新策略。

Plan Mode 重点参考：

- `/home/qt/claude-code-rev/src/tools/EnterPlanModeTool/`：判断何时进入 plan mode，并切换 permission mode。
- `/home/qt/claude-code-rev/src/tools/ExitPlanModeTool/ExitPlanModeV2Tool.ts`：读取 plan 文件、请求用户审批、审批后恢复权限。
- `/home/qt/claude-code-rev/src/tools/AskUserQuestionTool/`：Plan Mode 中用于澄清需求和取舍的交互型工具。
- `/home/qt/claude-code-rev/src/utils/plans.ts`：plan slug、plan 文件路径、resume/fork 计划恢复。
- `/home/qt/claude-code-rev/src/commands/plan/plan.tsx`：`/plan` 命令启用、显示和打开计划。
- `/home/qt/claude-code-rev/src/utils/messages.ts`：Plan Mode 的强提醒、迭代规划流程、稀疏提醒和退出规则。
- `/home/qt/claude-code-rev/src/bootstrap/state.ts`：plan mode 退出标记和退出后 attachment 状态。

## 推荐目录结构

```txt
bigcode/
  tasks/
    __init__.py
    models.py          # TaskItem、TaskStatus、输入输出类型
    store.py           # Task List 文件存储、锁、ID、CRUD、claim
    tools.py           # TaskCreate / TaskUpdate / TaskList / TaskGet
  plan/
    __init__.py
    mode.py            # PlanModeState、进入/退出、权限恢复
    store.py           # plan slug、plan 文件路径、读写
    tools.py           # EnterPlanMode / WritePlan / ExitPlanMode / PlanShow
```

和其他系统的关系：

- `tools/`：Task 工具和 Plan 工具都注册到 Tool registry，权限仍由统一权限系统判断。
- `hooks/`：Task / Plan 工具完成状态变更后触发 `TaskCreated`、`TaskUpdated`、`PlanModeEnter`、`PlanModeExit`；Plan reminder、approved plan reminder、Task reminder 和 Plan Stop 约束由内置 hooks 产生。
- `context/`：只负责把 hooks 返回的 attachment 转成 `<system-reminder>`，不直接实现 Task / Plan 生命周期副作用。
- `subagents/`：同步 subAgent 可复用 Task List；后台 task 生命周期不在本文 v1 范围内。
- `agent_loop.py`：只消费工具结果和 attachment，不直接操作任务文件。

## Task List 系统

### 目标

Task List 是执行阶段的结构化进度工具，服务三件事：

1. 让模型在复杂实现中可见地拆分工作。
2. 让用户看到当前进度。
3. 为后续 subAgent/team 模式保留 owner 和依赖能力。

Task List 不应该被 Plan Mode 强制触发。Plan Mode 结束、用户批准计划后，模型可以根据 approved plan 创建任务并逐项执行。

### 数据模型

```py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TaskStatus = Literal["pending", "in_progress", "completed"]


@dataclass
class TaskItem:
    id: str
    subject: str
    description: str
    status: TaskStatus = "pending"
    active_form: str | None = None
    owner: str | None = None
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
```

字段语义：

- `id`：task list 内递增字符串 ID，从 `1` 开始。
- `subject`：短标题，用祈使句描述结果，例如 `Fix login redirect`。
- `description`：足够让另一个 agent 独立理解的任务说明。
- `status`：只保留 `pending / in_progress / completed`。
- `active_form`：进行中 spinner 文案，例如 `Running tests`。
- `owner`：负责该任务的 agent ID 或名称；v1 可为空。
- `blocks`：当前任务完成前会阻塞的任务 ID。
- `blocked_by`：当前任务依赖的任务 ID。
- `metadata`：内部扩展字段，约定 `_internal=True` 的任务默认不展示。

### 文件布局

```txt
.bigcode/
  tasks/
    {task_list_id}/
      .lock
      .highwatermark
      1.json
      2.json
```

`task_list_id` 解析规则：

1. 显式传入的 task list ID。
2. 当前 team name，v1 可暂不启用。
3. 当前 session ID。
4. 非交互模式可从环境变量 `BIGCODE_TASK_LIST_ID` 覆盖。

路径必须经过安全清洗，只允许字母、数字、下划线和横线；其他字符替换为 `-`。

### 存储与锁

`TaskStore` 负责所有任务文件操作：

```py
class TaskStore:
    def create(self, task_list_id: str, data: CreateTaskInput) -> str: ...
    def get(self, task_list_id: str, task_id: str) -> TaskItem | None: ...
    def list(self, task_list_id: str) -> list[TaskItem]: ...
    def update(self, task_list_id: str, task_id: str, updates: UpdateTaskInput) -> TaskItem | None: ...
    def delete(self, task_list_id: str, task_id: str) -> bool: ...
    def claim(self, task_list_id: str, task_id: str, owner: str, check_busy: bool = False) -> ClaimResult: ...
    def reset(self, task_list_id: str) -> None: ...
```

关键规则：

- 创建任务必须持有 task-list 级锁，读取 `.highwatermark` 和现有文件最大 ID 后分配 `max + 1`。
- 删除任务前更新 `.highwatermark`，避免后续 ID 复用。
- 更新单任务时持有 task 文件锁。
- `claim(check_busy=True)` 必须持有 task-list 级锁，原子检查“任务未被别人占用、未完成、未被阻塞、owner 没有其他未完成任务”。
- `list()` 读取所有 JSON，按数字 ID 升序返回；坏文件要记录 debug 日志并跳过。

### 任务依赖

依赖关系保留 Claude Code 的双向字段：

- A `blocks=[B]` 表示 A 未完成会阻塞 B。
- B `blocked_by=[A]` 表示 B 依赖 A。

实现 `block_task(from_task_id, to_task_id)` 时必须同时更新两边字段，且去重。

可 claim 的任务定义：

- `status == "pending"`
- `owner is None` 或 owner 是当前 claimant
- `blocked_by` 里的任务都不存在，或都已 `completed`

### 工具接口

#### TaskCreate

输入：

```py
class TaskCreateInput(BaseModel):
    subject: str
    description: str
    active_form: str | None = None
    metadata: dict[str, Any] | None = None
```

行为：

- 创建 `pending` 任务。
- owner 为空。
- `blocks` / `blocked_by` 默认为空。
- 返回 `{ "task": { "id": id, "subject": subject } }`。
- 创建成功后触发 `HookBus.emit("TaskCreated")`，由 `TaskReminderHook` 刷新 reminder 状态。

#### TaskUpdate

输入：

```py
class TaskUpdateInput(BaseModel):
    id: str
    status: TaskStatus | None = None
    subject: str | None = None
    description: str | None = None
    active_form: str | None = None
    owner: str | None = None
    clear_owner: bool = False
    blocks: list[str] | None = None
    blocked_by: list[str] | None = None
```

行为：

- 只更新显式传入字段。
- `clear_owner=True` 时清空 owner，且优先级高于 `owner`。
- 如果 task 不存在，返回工具错误。
- 更新成功后触发 `HookBus.emit("TaskUpdated")`；设置 `completed` 后由 `TaskReminderHook` 刷新 task reminder。

#### TaskList

输入：

```py
class TaskListInput(BaseModel):
    status: TaskStatus | None = None
    owner: str | None = None
    pending_only: bool = False
```

行为：

- 返回当前 task list 的摘要列表。
- 默认包含所有非 internal 任务。
- `pending_only=True` 等价于过滤非 completed 任务。
- 输出按 ID 升序。

#### TaskGet

输入：

```py
class TaskGetInput(BaseModel):
    id: str
```

行为：

- 返回单个 task 的完整 JSON。
- 不存在时返回明确错误。

保留 `TaskList` 和 `TaskGet` 的原因：它们是低复杂度读取接口，没有它们模型无法可靠检查已有任务，容易重复创建或误判进度。

### 模型使用策略

系统 prompt 中给模型的规则：

- 多步骤实现、跨文件修改、需要用户看进度、或 approved plan 已进入执行阶段时，优先创建 Task List。
- Plan Mode 中不要为了规划本身创建 Task List；规划结果写进 plan 文件。
- 单步小改、纯问答、简单命令不要创建任务。
- 开始执行某个任务前，把它标为 `in_progress`。
- 完成后立即标为 `completed`，不要攒到最后批量更新。
- 发现新工作时创建新任务，不要把 scope 偷偷塞进旧任务。

## Plan Mode 系统

### 目标

Plan Mode 是复杂任务的只读规划状态，核心产物是一份可审批的 markdown plan 文件。

它的目的：

1. 允许模型在不修改 workspace 的前提下探索代码。
2. 在用户偏好、需求取舍或产品行为不明确时，用 `AskUserQuestion` 澄清。
3. 把发现、决策和最终方案持续写入 plan 文件。
4. 用 `ExitPlanMode` 把 plan 文件交给用户审批。
5. 审批通过后恢复执行权限，并把 approved plan 带入实现上下文。

Plan Mode 不是一句 prompt，而是权限模式、上下文提醒、计划文件和退出审批工具共同组成的流程。

### PlanModeState

```py
from dataclasses import dataclass
from typing import Literal

PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]


@dataclass
class PlanModeState:
    active: bool = False
    pre_plan_permission_mode: PermissionMode | None = None
    plan_file: str | None = None
    plan_slug: str | None = None
    approved_plan: str | None = None
    has_exited_plan_mode: bool = False
    needs_exit_attachment: bool = False
```

进入 plan mode 时记录原权限模式；退出时恢复原模式。若原模式不可用，默认恢复 `default`。

### Plan 文件

路径规则：

```txt
.bigcode/
  plans/
    {session_slug}.md
    {session_slug}-agent-{agent_id}.md
```

`PlanStore`：

```py
class PlanStore:
    def get_slug(self, session_id: str) -> str: ...
    def get_path(self, session_id: str, agent_id: str | None = None) -> Path: ...
    def read(self, session_id: str, agent_id: str | None = None) -> str | None: ...
    def write(self, session_id: str, content: str, agent_id: str | None = None) -> None: ...
    def clear_slug(self, session_id: str) -> None: ...
```

规则：

- slug 懒生成，并缓存到 session state。
- 文件名必须唯一；冲突时重试。
- 默认计划目录可配置，但必须限制在 workspace 或 BigCode home 下。
- v1 简化 resume：只要 transcript 里有 slug，就尝试读取本地 plan 文件；找不到则提示重新生成计划。

### EnterPlanMode 工具

输入为空：

```py
class EnterPlanModeInput(BaseModel):
    pass
```

行为：

1. 如果当前已经是 plan mode，直接返回提示。
2. 保存 `pre_plan_permission_mode`。
3. 设置权限模式为 `plan`。
4. 生成或获取 plan file path。
5. 触发 `HookBus.emit("PlanModeEnter")`；后续工作流提醒由 `PlanModeContextHook` 在 `ContextBuild` 时注入。

工具权限：

- 本身是 app state 变更工具。
- 不允许 subAgent 随意进入主会话 plan mode；subAgent 如需规划，使用自己的 plan file。

### Plan Mode 权限

`permission_mode="plan"` 下允许：

- 读工具：`Read`、`Glob`、`Grep`，以及只读 Bash，例如 `ls`、`find`、`grep`、`git status`、`git log`、`git diff`。
- 只读子 Agent：`explorer` 和 `planAgent`。它们只能搜索、读取和返回分析结果，不能写文件、调用 `ExitPlanMode`、再启动 Agent 或修改系统状态。
- `WritePlan`：唯一允许的写入口，只能写当前 plan file；`WritePlanInput` 只是该工具的输入 schema。
- `AskUserQuestion`：用于澄清需求、偏好、方案取舍和验收标准。
- `ExitPlanMode`：用于提交 plan 文件给用户审批。
- `/plan` 本地命令：用于显示或打开当前 plan 文件；不作为模型工具扩权。

禁止：

- 编辑 workspace 文件。
- 写非 plan 文件。
- 执行会修改文件、安装依赖、启动服务、发网络写请求的命令。
- 启动可写 subAgent。
- 启动普通实现型 subAgent、后台 subAgent，或允许子 Agent 继续启动子 Agent。
- 删除文件、迁移数据库、提交 git commit。

Bash 在 plan mode 下必须走只读分类器；无法分类时拒绝或询问。

### Plan Mode 工作流提示词

进入 Plan Mode 后，`PlanModeContextHook` 必须在 `ContextBuild` 时生成强提醒，Context 只负责渲染成 `<system-reminder>`。核心语义如下：

```txt
Plan mode is active. The user indicated that they do not want you to execute yet. You MUST NOT make any edits, run non-readonly tools, change configs, make commits, or otherwise change the system, except for the plan file.

Plan file:
- If a plan file already exists, read it and make incremental edits.
- If no plan file exists, create it at the specified path.
- This plan file is the ONLY file you may edit.
```

工作流采用 cc 的迭代规划思路：

1. **Explore**：用 Read/Glob/Grep 或只读 Bash 查代码，找现有函数、工具、模式和测试入口。
2. **Update the plan file**：每次有重要发现就立即写入或编辑 plan 文件，不要等全部探索完再写。
3. **Ask the user**：遇到代码无法回答的需求、偏好、取舍或边界问题，用 `AskUserQuestion`；不要问能从代码里查到的问题。
4. **Repeat**：继续探索、更新 plan、提问，直到计划决策完整。
5. **Exit**：计划准备好后调用 `ExitPlanMode`，不要用自然语言问“计划是否可以”。

第一次进入 Plan Mode 时建议模型快速扫描少量关键文件，形成初始理解后先写 skeleton plan，再开始提问；不要在完全不和用户互动的情况下做过度探索。

### explorer / planAgent 子 Agent

Claude Code 的 plan mode 提示词允许主 Agent 在规划时调用内置只读子 Agent 辅助探索和设计。BigCode v1 可以复现这个核心形态，但必须保持权限收窄：

- `explorer` agent：只负责快速搜索和阅读代码，适合范围不确定、涉及多处代码、需要并行搜索现有实现时使用。
- `planAgent` agent：只负责基于主 Agent 已探索到的背景设计实现方案，适合需要验证理解、比较取舍或获得第二视角时使用。
- 两类 agent 都继承 `permission_mode="plan"`，不能写 workspace，不能写 plan 文件，不能调用 `ExitPlanMode`，不能再启动子 Agent。
- 子 Agent 的输出只是参考材料；主 Agent 必须自己审阅关键文件、判断是否符合用户意图，并负责最终写入 plan 文件。
- 对简单、路径明确、单文件或小范围任务，直接用读工具即可，不必强制启动子 Agent。

可选的五阶段流程：

1. **Initial Understanding**：主 Agent 读用户需求，必要时并行启动少量 `explorer` agent 搜索相关代码、现有模式和测试入口。
2. **Design**：主 Agent 可启动 `planAgent` agent，让它基于 Phase 1 背景输出详细方案、关键文件和取舍。
3. **Review**：主 Agent 阅读子 Agent 提到的关键文件，确认方案符合用户原始意图；有歧义就用 `AskUserQuestion`。
4. **Final Plan**：主 Agent 调用 `WritePlan` 写最终 markdown plan，只保留推荐方案。
5. **ExitPlanMode**：主 Agent 调用 `ExitPlanMode` 请求审批。

### Plan 文件结构

plan 文件用 markdown，内容应简洁但足够可执行：

- `Context`：一两句说明为什么要改、目标是什么。
- `Implementation`：只写推荐方案，不列所有备选路线。
- `Files`：列出关键文件路径，以及每个文件要改什么。
- `Reuse`：列出现有可复用函数、工具或模式，带文件路径。
- `Verification`：说明如何端到端验证，优先给出具体命令或手动流程。

计划完成标准：

- 已解决所有高影响歧义。
- 写清楚要改什么。
- 写清楚关键文件和复用点。
- 写清楚如何验证。
- 没有把实现步骤写成空泛路线图。

### Plan 文件更新工具

v1 可以提供一个普通 `WritePlan` 工具，也可以允许 `WriteFile` 在 plan mode 下只写 plan file。

建议实现 `WritePlan`，避免路径权限绕来绕去：

```py
class WritePlanInput(BaseModel):
    content: str
```

行为：

- 覆盖当前 session plan file。
- 返回 plan path 和字符数。
- 可触发 plan file snapshot attachment；具体注入由 `PlanModeContextHook` 或后续专门 hook 管理，不由 `WritePlan` 直接拼 API 消息。

如果复用 `WriteFile` / `EditFile`，权限层必须硬性限制目标只能是当前 plan file。

### PlanShow 工具

输入为空：

```py
class PlanShowInput(BaseModel):
    pass
```

行为：

- 校验当前存在 plan file path。
- 读取当前 plan 文件；不存在时返回空内容和 path，不创建文件。
- 返回 `{ "path": path, "content": content or "" }`。
- 不修改权限模式，不请求审批，不写 workspace。
- 主要服务 `/plan` 显示、调试和恢复上下文，不作为模型请求用户批准的机制。

### ExitPlanMode 工具

输入：

```py
class ExitPlanModeInput(BaseModel):
    allowed_prompts: list[AllowedPrompt] | None = None
```

v1 可以先忽略 `allowed_prompts`，仅保留字段兼容。

行为：

1. 校验当前是 plan mode。
2. 读取当前 plan file。
3. 如果 plan 为空，返回错误：必须先写计划。
4. 向用户展示 plan，并请求审批。
5. 如果用户拒绝，保持 plan mode，并把反馈作为 user message 进入下一轮。
6. 如果用户批准，保存 `approved_plan`。
7. 恢复 `pre_plan_permission_mode`。
8. 设置 `needs_exit_attachment=True`。
9. 触发 `HookBus.emit("PlanModeExit")`；下一轮 `ContextBuild` 由 `PlanModeContextHook` 注入 approved plan。

退出规则：

- Plan Mode 中模型一轮结束只能是 `AskUserQuestion` 或 `ExitPlanMode`；该约束由 `PlanModeStopHook` 在 `Stop` 事件中强制，必要时要求模型续跑一轮。
- `AskUserQuestion` 只能用来澄清需求或选择方案，不用于询问“计划是否通过”。
- `ExitPlanMode` 本身就是请求用户审批计划的机制。

非交互模式：

- 默认不能卡住等待 TUI 审批。
- 可以返回 `requires_approval` 状态，由外层 SDK 决定批准或拒绝。
- 如果没有审批通道，拒绝退出并提示用户切换交互模式或传入显式 approval。

### `/plan` 命令

本地命令行为：

- 当前不在 plan mode：切换到 plan mode。
- 当前在 plan mode 且没有参数：显示当前 plan 文件内容。
- `/plan open`：用外部编辑器打开 plan 文件；v1 可先只打印路径。
- `/plan clear`：可选，清空当前 plan 文件但保持 plan mode。

### Hooks / Context 注入

Plan Mode 要通过 Hooks 在 Context 中出现三类提醒。Plan / Task 模块只维护状态，`PlanModeContextHook` 和 `PlanModeStopHook` 负责生成提醒和续跑要求。

#### system prompt 动态规则

当 active：

```txt
You are in Plan Mode. Read and inspect as needed, but do not edit workspace files or execute mutating commands. Write the final implementation plan to {plan_file}. End planning by calling AskUserQuestion for clarifications or ExitPlanMode for approval.
```

#### 周期性 attachment

当 plan mode 持续多轮时注入：

```txt
<system-reminder>
Plan mode still active. Read-only except plan file ({plan_file}). Continue the iterative workflow: explore codebase, ask user questions for unresolved decisions, and write to plan incrementally. End turns with AskUserQuestion or ExitPlanMode. Never ask about plan approval via text or AskUserQuestion.
</system-reminder>
```

#### 退出后一次性 attachment

审批通过后的下一轮由 `PlanModeContextHook` 注入：

```txt
<system-reminder>
Plan Mode has ended. You may now implement the approved plan.

Approved plan:
{approved_plan}
</system-reminder>
```

注入后清除 `needs_exit_attachment`，避免重复。

## Agent Loop 集成

推荐主流程：

```txt
用户输入
  -> agent loop 追加 user message
  -> HookBus 执行 UserPromptSubmit / ContextBuild
  -> Context 构建 system prompt + hooks 产出的 plan/task reminders
  -> 模型返回 tool_use
  -> Tool runner 执行 Task 工具、EnterPlanMode、WritePlan、PlanShow、ExitPlanMode、AskUserQuestion
  -> Task / Plan 工具触发 TaskCreated / TaskUpdated / PlanModeEnter / PlanModeExit hooks
  -> 工具结果进入 messages
  -> 如果 ExitPlanMode approved，下一轮 ContextBuild 注入 approved plan reminder
  -> Stop 时 PlanModeStopHook 校验本轮是否正确结束
  -> 实现阶段正常执行工具，并按需使用 Task List 跟踪进度
```

工具注册要求：

- Task List 工具 `state_effect="app_state"`，默认串行。
- `EnterPlanMode` / `ExitPlanMode` `state_effect="app_state"`，不能并发执行。
- `WritePlan` 只允许写当前 plan file，不能泛化成任意写文件。

## 省略与后置

v1 明确不做：

- Background Task 系统。
- `ultraplan` 远程 Claude Code session。
- Web UI 里编辑 plan 后同步回终端。
- `VerifyPlanExecution` 背景 verifier。
- TeamCreate / teammate mailbox / SendMessage plan approval。
- SDK 级 task_started / task_completed 事件。
- 复杂 TaskCreated rollback / TaskCompleted rollback；v1 只通过 hooks 刷新提醒和记录状态。
- 复杂 TUI 展开、保留、隐藏和焦点导航。
- fork session 时复制 plan 文件。

保留接口但后置：

- `allowed_prompts`。
- `agent_id` 专属 plan file。
- task list watcher。
- transcript 中 plan file snapshot recovery。

## 测试计划

Task List：

- 创建任务生成 `1.json`，再次创建生成 `2.json`。
- 删除最大 ID 后，新任务 ID 不复用。
- `TaskUpdate` 只更新传入字段。
- `TaskList` 返回按 ID 升序的摘要列表，并过滤 internal 任务。
- `TaskGet` 返回完整 task JSON；不存在时返回明确错误。
- `block_task(A, B)` 同时更新 A.blocks 和 B.blocked_by。
- B 被未完成 A 阻塞时 claim 失败；A completed 后 claim 成功。
- `claim(check_busy=True)` 在 owner 已有未完成任务时失败。
- 坏 JSON 文件不会让 `TaskList` 整体失败。

Plan Mode：

- `EnterPlanMode` 保存原权限模式，并设置 mode 为 plan。
- 进入 plan mode 后，`PlanModeContextHook` 注入完整工作流提醒。
- plan mode 下 workspace 写文件被拒绝。
- plan mode 下 `WritePlan` 成功写 plan file。
- plan 文件可被增量更新。
- 没有 plan 文件时 `ExitPlanMode` 返回错误。
- 拒绝审批后仍保持 plan mode。
- 批准审批后恢复原权限模式，并由 `PlanModeContextHook` 注入 approved plan reminder。
- `/plan` 能显示当前 plan path 和内容。

集成：

- 一个复杂请求可以按顺序完成：EnterPlanMode -> 探索 -> WritePlan skeleton -> AskUserQuestion -> Edit/WritePlan final -> ExitPlanMode -> approved -> TaskCreate -> TaskUpdate in_progress -> 实现 -> TaskUpdate completed。
- Plan Mode 中不会因为“正在规划”而强制创建 Task List。

## 实现顺序

1. 实现 `tasks.models` 和 `tasks.store`，先跑通文件型 Task List。
2. 注册 `TaskCreate / TaskUpdate / TaskList / TaskGet` 工具。
3. 实现 `plan.store` 和 `plan.mode`，接入 permission mode。
4. 注册 `EnterPlanMode / WritePlan / ExitPlanMode / PlanShow`。
5. 注册 Plan / Task 内置 hooks：`PlanModeContextHook`、`PlanModeStopHook`、`TaskReminderHook`。
6. 通过 `ContextBuild` hook 接入完整 Plan Mode 工作流提醒。
7. 实现 approved plan 注入后的执行阶段 Task List 使用策略。
7. 后续再考虑 task watcher、后台 task 生命周期和更完整 resume。

## 验收标准

BigCode 完成 v1 后，应满足：

- 模型能主动创建和更新任务清单，用户能查询任务状态。
- 复杂修改能进入只读 Plan Mode，产出可审阅的 markdown 计划文件。
- Plan Mode 中未审批前无法修改 workspace。
- Plan Mode 中模型会边探索边更新 plan 文件，而不是只在最后口头总结。
- 审批后模型能拿到 approved plan 并继续实现。
- Task List 只作为执行阶段进度追踪，不是 Plan Mode 的必需产物。
