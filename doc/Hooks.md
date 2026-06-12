# Hooks 系统设计（Python 版）

## 总纲

BigCode 的 Hooks 系统不是简单的“用户脚本触发器”，而是核心生命周期总线。它负责把当前各系统里分散的副作用收口到一个可审计、可测试的入口：

- 动态上下文提醒注入。
- 用户输入提交前拦截。
- 工具执行前后拦截。
- Plan Mode 的工作流提醒和 Stop 续跑。
- Task 生命周期提醒。
- MCP / Skill Capability Index 增量注入。
- Compact 前后状态恢复。
- SubAgent 启停状态记录。

Hooks 不取代各模块自己的核心职责：

- Context 仍是消息、attachment 投影、API normalizer 和 transcript 的事实来源。
- Tool 仍负责 schema 校验、权限、路径安全、并发、执行和输出上限。
- Task / Plan 仍负责业务状态、计划文件和审批。
- MCP / Skill 仍负责能力发现、注册、权限目标和外部调用。
- Compact 仍负责上下文裁剪和摘要算法。
- SubAgent 仍负责独立 agent loop、工具池和 sidechain transcript。

Hooks 只负责“什么时候触发、触发后如何合并结果、哪些结果能影响下一步流程”。

对应 Claude Code 源码参考：

- `/home/qt/claude-code-rev/src/schemas/hooks.ts`：用户 hooks 配置 schema。
- `/home/qt/claude-code-rev/src/utils/hooks.ts`：hook 输入、执行、JSON 输出解析和事件执行器。
- `/home/qt/claude-code-rev/src/services/tools/toolHooks.ts`：`PreToolUse` / `PostToolUse` 与工具执行和权限的接入。
- `/home/qt/claude-code-rev/src/query/stopHooks.ts`：`Stop` / `SubagentStop` hooks 与停止续跑逻辑。
- `/home/qt/claude-code-rev/src/entrypoints/sdk/coreSchemas.ts`：hook event 和 hook input 的 SDK 形态。

BigCode v1 只复现核心业务形态，不照搬 cc 的完整复杂度。prompt/http/agent hooks、async hooks、plugin managed hooks、复杂 SDK control hooks、frontmatter hooks 都后置。

## 推荐目录结构

```txt
bigcode/
  hooks/
    __init__.py
    models.py        # HookEvent、HookInput、HookOutput、HookDecision
    bus.py           # HookBus：注册、排序、执行、聚合
    builtins.py      # BigCode 内置 lifecycle hooks
    settings.py      # .bigcode/settings.json 用户 hooks 配置读取
    matcher.py       # event / tool / task / capability matcher
    command.py       # 用户 command hook 执行器
```

与其他系统的关系：

- `context/`：构建上下文时触发 `ContextBuild`，消费 hooks 返回的 attachments。
- `tools/`：Tool Runner 在工具前后触发 `PreToolUse` / `PostToolUse`。
- `tasks/` 和 `plan/`：状态写入后触发 `TaskCreated` / `TaskUpdated` / `PlanModeEnter` / `PlanModeExit`。
- `context/compact.py`：压缩前后触发 `PreCompact` / `PostCompact`。
- `subagents/`：子 Agent 启停触发 `SubagentStart` / `SubagentStop`，但 v1 不开放用户自定义 subagent hooks。
- `mcp_skill/`：能力集合变化后触发 `CapabilityChanged`，由 hooks 决定是否注入 capability attachment。

## 事件模型

v1 事件集：

```py
from typing import Literal

HookEvent = Literal[
    "SessionStart",
    "UserPromptSubmit",
    "ContextBuild",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "PlanModeEnter",
    "PlanModeExit",
    "TaskCreated",
    "TaskUpdated",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "CapabilityChanged",
]
```

事件分级：

| 类别 | 事件 | 允许影响 |
|------|------|----------|
| blocking hooks | `UserPromptSubmit`、`PreToolUse`、`Stop`、`PlanModeExit`、`TaskUpdated` | 阻断、要求询问、改写输入、要求续跑 |
| context hooks | `SessionStart`、`ContextBuild`、`PostToolUse`、`CapabilityChanged`、`PostCompact` | 返回 attachment / system reminder |
| notification hooks | `TaskCreated`、`SubagentStart`、`SubagentStop` | 写 transcript、刷新状态、返回弱提示 |
| maintenance hooks | `PreCompact`、`PostCompact` | 保存/恢复状态、生成 compact 相关 attachment |

## 核心类型

```py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


HookDecision = Literal["approve", "ask", "block", "passthrough"]
HookSource = Literal["built-in", "user"]


@dataclass
class HookInput:
    hook_event_name: HookEvent
    session_id: str
    cwd: str
    permission_mode: str
    transcript_path: str | None = None
    agent_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookOutput:
    decision: HookDecision = "passthrough"
    reason: str = ""
    updated_input: dict[str, Any] | None = None
    additional_context: str | None = None
    attachments: list["Attachment"] = field(default_factory=list)
    continue_turn: bool | None = None
    stop_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResult:
    event: HookEvent
    source: HookSource
    name: str
    output: HookOutput
    duration_ms: int
    error: str | None = None
```

`payload` 按事件携带专属字段：

- `UserPromptSubmit`：`prompt`、`pasted_content_ids`。
- `ContextBuild`：`user_input`、`message_count`、`active_capabilities`、`plan_mode_state`、`task_state`。
- `PreToolUse`：`tool_name`、`tool_input`、`tool_use_id`、`tool_summary`。
- `PostToolUse`：`tool_name`、`tool_input`、`tool_use_id`、`tool_result`、`is_error`。
- `Stop`：`stop_reason`、`last_assistant_text`、`turn_tool_names`、`plan_mode_state`。
- `PlanModeEnter` / `PlanModeExit`：`plan_file`、`approved_plan`、`approval_result`。
- `TaskCreated` / `TaskUpdated`：`task`、`previous_task`、`task_list_id`。
- `PreCompact` / `PostCompact`：`utilization`、`compact_level`、`compact_result`。
- `SubagentStart` / `SubagentStop`：`agent_id`、`agent_name`、`sidechain_path`、`result_summary`。
- `CapabilityChanged`：`added`、`removed`、`capability_state`.

## HookBus

`HookBus` 是唯一事件调度入口。各模块不能直接调用其他模块的生命周期副作用函数。

```py
class HookHandler:
    name: str
    source: HookSource
    events: tuple[HookEvent, ...]
    priority: int = 100

    async def matches(self, input: HookInput) -> bool: ...
    async def run(self, input: HookInput) -> HookOutput: ...


class HookBus:
    def register(self, handler: HookHandler) -> None: ...
    async def emit(self, event: HookEvent, input: HookInput) -> "HookAggregate": ...
```

聚合规则：

- handler 按 `priority` 升序执行。
- 内置 safety hooks 优先于用户 hooks。
- 同一事件中任一 hook 返回 `block`，事件聚合为阻断。
- `ask` 优先级低于 `block`，高于 `approve`。
- 多个 `additional_context` 按执行顺序合并成多个 attachment，不拼成一个大字符串。
- `updated_input` 只允许 `PreToolUse` 使用；多个 hook 同时改写时，后一个基于前一个结果继续处理。
- hook 执行异常默认不让进程崩溃，转为 `hook_execution_error` attachment；blocking 事件中的内置 safety hook 异常按 fail closed 处理。
- `Stop` 事件每个 assistant turn 最多允许一次 continuation，防止无限续跑。

```py
@dataclass
class HookAggregate:
    event: HookEvent
    results: list[HookResult]
    decision: HookDecision = "passthrough"
    reason: str = ""
    updated_input: dict[str, Any] | None = None
    attachments: list["Attachment"] = field(default_factory=list)
    continue_turn: bool = False
    stop_reason: str | None = None
```

## 内置 Hooks

内置 hooks 是 BigCode 核心业务的一部分，使用同一套 `HookBus` 注册和执行。

### PlanModeContextHook

监听：`ContextBuild`、`PlanModeExit`

职责：

- Plan Mode active 时返回 `plan_mode` attachment。
- `needs_exit_attachment=True` 时返回 `plan_mode_exit` attachment，内容包含 approved plan。
- 输出后清除一次性 `needs_exit_attachment`，避免重复注入。

### PlanModeStopHook

监听：`Stop`

职责：

- Plan Mode 下，如果本轮没有调用 `AskUserQuestion` 或 `ExitPlanMode`，返回 `block` 和 `continue_turn=True`。
- 反馈内容要求模型继续只读探索、写 plan，或调用正确的交互/退出工具。
- 不允许自然语言询问“计划是否可以执行”；计划审批必须走 `ExitPlanMode`。

### ToolHookBridge

监听：`PreToolUse`、`PostToolUse`

职责：

- 把用户 `PreToolUse` command hook 的 `decision`、`updated_input`、`additional_context` 转为 Tool Runner 可消费的结果。
- `approve` 只能跳过普通交互提示，不能绕过 hard deny、explicit deny、Plan Mode、路径安全或工具约束。
- `updated_input` 必须让 Tool Runner 重新执行 schema 校验和权限判断。

### TaskReminderHook

监听：`TaskCreated`、`TaskUpdated`、`ContextBuild`

职责：

- 任务创建或更新后刷新 task reminder 状态。
- `ContextBuild` 时根据当前未完成任务生成 `todo_reminder` attachment。
- v1 不做复杂 task rollback；如果 `TaskUpdated(completed)` hook 失败，只生成错误 attachment，不回滚任务状态。

### CapabilityIndexHook

监听：`SessionStart`、`CapabilityChanged`、`ContextBuild`

职责：

- 维护每个 session / agent 已注入能力集合。
- 只注入新增 MCP / Skill / external prompt / external resource 能力。
- resume 时根据 transcript 中已有 `capability_index` attachment 防重复。

### InstructionRestoreHook

监听：`SessionStart`、`PostCompact`

职责：

- 会话启动时加载 BigCode instruction files 的状态。
- compact 后确保关键 instruction / capability 提醒仍可通过后续 `ContextBuild` 恢复。
- 不直接拼 API messages，只返回或刷新 attachment 状态。

### StopContinuationHook

监听：`Stop`

职责：

- 聚合 Stop hooks 的 blocking feedback。
- 若需要继续，生成 system reminder 并要求 agent loop 再跑一轮。
- 每个 assistant turn 只允许一次续跑。

### SubagentLifecycleHook

监听：`SubagentStart`、`SubagentStop`

职责：

- 写入 sidechain transcript 生命周期状态。
- 同步必要的 `ReadFileState` 写后快照到父上下文。
- 生成父级 `AgentTool` result metadata；不把子 Agent 完整中间消息注入父 messages。

## 用户 Command Hooks

用户 hooks 配置在 `.bigcode/settings.json`：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python scripts/check_bash.py",
            "timeout": 30
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python scripts/final_check.py"
          }
        ]
      }
    ]
  }
}
```

v1 只支持：

```py
@dataclass
class CommandHook:
    type: Literal["command"]
    command: str
    timeout: int = 60
    once: bool = False
    status_message: str | None = None
```

matcher 规则：

- `matcher` 为空：匹配该事件全部输入。
- `PreToolUse` / `PostToolUse`：按工具名匹配，例如 `Bash`、`Read`、`Edit`、`Write`、`Agent`。
- `TaskCreated` / `TaskUpdated`：按 task status 或 task subject 前缀匹配可后置；v1 只做空 matcher。
- v1 不实现 cc 的 permission rule 语法，例如 `Bash(git *)`；后续可扩展为 `tool(pattern)`。

执行规则：

- command 在当前 `cwd` 下执行。
- hook input 以 JSON 写入 stdin。
- stdout / stderr 分别限制大小，建议 64 KiB。
- 默认 timeout 60 秒。
- exit code `0` 表示通过。
- exit code `2` 表示阻断，stderr 或 stdout 作为 reason。
- 其他非零退出码表示 hook 执行错误，生成 `hook_execution_error` attachment；blocking 事件中不默认当作用户阻断。

stdout 最后一段如果是 JSON，可解析为结构化输出：

```json
{
  "decision": "approve | ask | block",
  "reason": "human readable reason",
  "updated_input": {},
  "additional_context": "text to inject as system-reminder",
  "continue": true
}
```

## 接入流程

Agent loop：

```txt
startup
  -> HookBus.emit(SessionStart)
user prompt
  -> append UserMessage
  -> HookBus.emit(UserPromptSubmit)
context build
  -> apply_context_compact
  -> HookBus.emit(ContextBuild)
  -> Context consumes attachments
model / tool loop
  -> Tool Runner emits PreToolUse / PostToolUse
no tool_use
  -> HookBus.emit(Stop)
  -> final answer or one continuation turn
```

Tool Runner：

```txt
parse tool_use
  -> schema validate
  -> HookBus.emit(PreToolUse)
  -> apply updated_input and schema validate again
  -> unified permission engine
  -> execute tool
  -> HookBus.emit(PostToolUse)
  -> ToolRunResult
```

Plan Mode：

```txt
EnterPlanMode
  -> update PlanModeState
  -> HookBus.emit(PlanModeEnter)

ExitPlanMode approved
  -> save approved_plan
  -> restore permission mode
  -> set needs_exit_attachment=True
  -> HookBus.emit(PlanModeExit)
```

Task：

```txt
TaskCreate / TaskUpdate writes app state
  -> HookBus.emit(TaskCreated / TaskUpdated)
  -> TaskReminderHook refreshes reminder state
```

Compact：

```txt
apply_context_compact
  -> HookBus.emit(PreCompact)
  -> compact algorithm
  -> HookBus.emit(PostCompact)
```

SubAgent：

```txt
AgentTool starts subagent
  -> HookBus.emit(SubagentStart)
subagent finalizes
  -> HookBus.emit(SubagentStop)
  -> parent receives AgentTool result only
```

## 冲突和权限规则

Hooks 可以：

- 添加上下文 attachment。
- 阻断用户输入、工具执行或 Stop 结束。
- 要求询问用户。
- 改写 `PreToolUse` 的工具输入。
- 请求 Stop 后多跑一轮。

Hooks 不能：

- 绕过 hard deny。
- 绕过 explicit deny。
- 绕过 Plan Mode 的只读限制。
- 绕过路径安全、敏感文件、SSRF、MCP / Skill 注册目录限制。
- 直接构造 API messages。
- 在 v1 内部调用 BigCode tools。
- 扩大 subAgent 的 workspace、权限模式或工具池。

用户 hook 的 `approve` 语义：

- 可以表示“这个 hook 不反对”。
- 可以为普通 ask 场景提供一个跳过交互的理由。
- 不能覆盖 hard deny、显式 deny、工具约束、非交互拒绝和 Plan Mode 限制。

## Transcript 和 Resume

Hook 执行结果应进入 transcript，但不能撑爆上下文：

- 保存 event、handler name、source、duration、decision、reason 摘要。
- 保存 stdout/stderr 截断摘要，不保存无限输出。
- 保存 generated attachments 的类型和摘要。
- `once=True` 的用户 hook v1 可以只做 session 内存级去重；是否回写 settings 后置。
- resume 时 CapabilityIndexHook、PlanModeContextHook 等内置 hooks 必须能通过 transcript 判断哪些一次性 attachment 已经发送。

## 测试计划

HookBus：

- handler 按 priority 顺序执行。
- `block` 聚合优先于 `ask` / `approve`。
- 多个 attachment 按顺序返回。
- handler 异常生成可追踪错误，不导致非 blocking 事件崩溃。

用户 command hooks：

- stdin JSON 包含正确 event 和 payload。
- exit `0` 成功。
- exit `2` 阻断。
- timeout 被终止并生成错误。
- 非法 JSON 不影响纯 stdout/stderr exit code 语义。
- stdout/stderr 超限会截断。

Context 集成：

- `ContextBuild` 通过 hooks 生成 plan reminder、task reminder、capability index。
- Context normalizer 仍负责把 attachment 转成 `<system-reminder>`。
- resume 后 capability index 不重复注入。

Tool 集成：

- `PreToolUse` hook 能阻断 `Bash`，命令不执行。
- `PreToolUse` hook 能改写输入，Tool Runner 会重新 schema 校验和权限判断。
- hook approve 不能绕过 hard deny 或 explicit deny。
- `PostToolUse` hook 能追加 context attachment。

Plan / Task 集成：

- `EnterPlanMode` 触发 `PlanModeEnter`。
- Plan Mode 下普通 Stop 会被 `PlanModeStopHook` 要求继续，除非本轮调用了 `AskUserQuestion` 或 `ExitPlanMode`。
- `ExitPlanMode` approved 后只注入一次 approved plan reminder。
- `TaskUpdated(completed)` 刷新 task reminder。

Compact / SubAgent 集成：

- `PreCompact` / `PostCompact` 在每次 compact 前后执行。
- compact 后 instruction 和 capability reminder 能恢复。
- `SubagentStart` / `SubagentStop` 写 sidechain 生命周期状态，不污染父 messages。

## 实现顺序

1. 实现 `hooks.models` 和 `HookBus`，先只支持内置 hooks。
2. 将 Context attachment 收集改为 `ContextBuild` hooks。
3. 将 Tool Runner 接入 `PreToolUse` / `PostToolUse`。
4. 将 Plan Mode reminder、approved plan reminder 和 Stop 约束迁到内置 hooks。
5. 将 Task reminder、Capability Index、Compact restore 接入内置 hooks。
6. 增加用户 command hook 配置和执行器。
7. 最后接入 SubAgent lifecycle hooks。
