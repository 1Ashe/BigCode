# BigCode 设计总览

BigCode 是一个用 Python 实现的简化版 Claude Code。目标不是完整复刻所有复杂分支，而是用尽量清晰、可测试的代码实现 coding agent 的核心闭环：上下文构建、工具调用、权限控制、Hooks 生命周期、长上下文压缩、计划模式和子 Agent 委派。

## Glossary / Canonical Names

跨文档实现时以下名称为唯一规范写法。其他文档如果为了说明边界重复提到这些概念，应以本节和对应权威文档为准。
API接口采用claude格式而不是openai格式

| 类别 | 规范名称 |
|------|----------|
| 权限模式 | `default`、`acceptEdits`、`plan`、`bypassPermissions` |
| 权限决策结果 | `allow`、`deny`、`ask`、`passthrough`；`ask` 不是权限模式 |
| 内置 subAgent | `general-purpose`、`explorer`、`code-reviewer`、`planAgent` |
| Plan Mode 只读 subAgent | `explorer`、`planAgent` |
| 内部消息类型 | `user`、`assistant`、`system`、`attachment`、`progress`、`context_summary`、`snip_boundary` |
| MCP-backed tool | 对模型暴露为普通工具名；MCP server / remote tool name 只存在于 Tool Registry route metadata |
| 外部 resource 工具 | `ExternalResourceList`、`ExternalResourceRead` |
| 外部 prompt 工具 | `ExternalPromptList`、`ExternalPromptGet` |
| Skill 工具 | `SkillLoad`、`SkillResourceRead` |
| Task 工具 | `TaskCreate`、`TaskUpdate`、`TaskList`、`TaskGet` |
| Plan / 交互工具 | `EnterPlanMode`、`WritePlan`、`PlanShow`、`ExitPlanMode`、`AskUserQuestion` |
| Hooks 生命周期事件 | `SessionStart`、`UserPromptSubmit`、`ContextBuild`、`PreToolUse`、`PostToolUse`、`Stop`、`PlanModeEnter`、`PlanModeExit`、`TaskCreated`、`TaskUpdated`、`PreCompact`、`PostCompact`、`SubagentStart`、`SubagentStop`、`CapabilityChanged` |

跨文档权威来源：

- 配置目录、加载优先级、模型注册表、用户配置和运行态目录边界以 `Config.md` 为准。
- 权限模式、权限顺序、路径安全、工具并发和执行侧输出上限以 `Tool.md` 为准。
- Plan Mode 生命周期、计划文件、退出审批和 Task List 业务状态以 `TaskPlan.md` 为准。
- 消息模型、API 归一化、attachment、tool_result 映射和 transcript / resume 以 `Context.md` 为准。
- 长上下文压缩算法细节以 `memory-compact-deep-dive.md` 为准；`Context.md` 只保留接口和接入点。
- SubAgent 内部流程、AgentDefinition、工具池收窄、sidechain transcript 和后台 task 以 `subAgent.md` 为准。
- MCP / Skill 能力发现、加载、权限目标和 capability index 以 `MCP-Skill.md` 为准。
- Hooks 事件、内置 lifecycle hooks、用户 command hook、事件聚合和跨系统副作用归属以 `Hooks.md` 为准。

这个目录下的文档需要作为一个整体阅读。各文档分工如下：

1. `Config.md`：定义全局 / 项目 / cwd-local 配置目录、加载和合并规则、模型注册表、用户配置文件和运行态目录边界。
2. `Context.md`：定义内部 `messages`、`context_messages`、`api_messages` 三层消息模型，以及 system prompt、attachment、tool_result 映射、API 前归一化、transcript 和 resume。
3. `Tool.md`：定义工具协议、工具注册、权限模型、路径安全、`ReadFileState`、并发调度、输出上限，以及工具层如何接入 app state 工具。
4. `TaskPlan.md`：定义 Task List 和 Plan Mode，复现 Claude Code 的任务清单、计划文件、只读规划流程和退出审批。
5. `Hooks.md`：定义 HookBus、事件模型、内置 lifecycle hooks、用户 command hooks，以及 Context / Tool / Plan / Task / Compact / SubAgent / MCP / Skill 的生命周期副作用如何统一接入。
6. `memory-compact-deep-dive.md`：是 `Context.md` 中 BigCode 目标模块 `bigcode/context/compact.py` 的深入设计，详细解释 Micro Compact、Snip Compact、Context Collapse、Auto Compact 的触发阈值、候选区间和状态管理。
7. `subAgent.md`：定义 `AgentTool`、`AgentDefinition`、`SubAgentContext`、子 Agent 工具池、权限继承、sidechain transcript 和后台 task。
8. `MCP-Skill.md`：定义 FastMCP client 接入、本地 Skill 注册、Capability Index、权限收窄和与 Tool / Context / SubAgent 的集成。
9. `BigCode.md`：当前总入口，说明系统边界、文档关系和推荐实现顺序。

文档维护规则：

- BigCode 目标模块路径使用 `bigcode/...` 或当前文档中的 Python 文件名。
- Claude Code 参考实现必须写成 `/home/qt/claude-code-rev/...` 下的真实路径。
- 不要把 BigCode 目标模块名写成 Claude Code 源码路径；找不到对应 cc 文件时，要明确标注为 BigCode v1 自己的目标模块。

## 系统关系

核心运行链路如下：

```txt
用户输入
  -> 主 Agent Loop 追加 UserMessage 到 messages
  -> HookBus 执行 UserPromptSubmit / ContextBuild 等生命周期事件
  -> Context 构建 system_prompt + api_messages
  -> 模型返回 assistant message / tool_use
  -> HookBus 执行 PreToolUse / PostToolUse
  -> Tool Runner 执行工具并返回 ToolRunResult
  -> Context 将 ToolRunResult 映射为 tool_result message
  -> HookBus 执行 Stop，必要时要求再跑一轮
  -> 回到下一轮模型调用，直到没有 tool_use
```

几个系统的边界要保持清楚：

- Context 系统是消息和 API payload 的事实来源。Tool 和 SubAgent 不直接拼 API messages。
- Tool 系统只负责执行、权限、路径、并发、输出上限和 `ToolRunResult`，不直接构造 `UserMessage` 或 `ToolResultBlock`。
- Hooks 系统是生命周期副作用总线。Context / Tool / Task / Plan / Compact / SubAgent / MCP / Skill 不互相直接调用对方的提醒、校验或续跑逻辑，统一通过 `HookBus` 触发和聚合。
- Memory Compact 是 Context 的子模块，在构建 `context_messages` 前运行，输出投影后的 `projected_messages`。
- SubAgent 通过 Tool 系统里的 `AgentTool` 启动，但内部仍复用主 Agent Loop、Context 构建和 Tool Runner。
- Task/Plan 是 Tool 系统上的 app state 能力，但业务状态、持久化和 Plan Mode 生命周期按 `TaskPlan.md` 独立实现。
- MCP 和 Skill 是能力接入层：MCP 通过 FastMCP Client 消费外部 server，Skill 从本地注册目录加载指令和资源；二者都必须经过 Tool 权限、输出预算和 Context capability index，不能直接修改 API messages。

## 推荐实现顺序

1. 先实现 `Config.md` 中的配置加载、合并、校验和模型注册表，输出 `RuntimeConfig`。
2. 实现 Context 基础消息模型、system prompt、attachment、normalizer 和 transcript，并消费已解析的模型能力。
3. 实现 `Hooks.md` 中的 `HookBus`、核心类型和内置 hooks 注册机制，先不开放用户 command hooks。
4. 再实现 Tool registry、permissions、paths、`ReadFileState`、runner 和基础文件/Bash/搜索工具，并接入 `PreToolUse` / `PostToolUse`。
5. 接入主 Agent Loop，跑通 assistant `tool_use` 到 `ToolRunResult` 再回写 `messages` 的闭环，并接入 `UserPromptSubmit`、`ContextBuild`、`Stop`。
6. 实现 `TaskPlan.md` 中的 Task List、Plan Mode 和 `AskUserQuestion`，把 Plan reminder、approved plan reminder、Task reminder、Plan Stop 约束放入内置 hooks。
7. 实现 `MCP-Skill.md` 中的 Skill registry、SkillLoad、Capability Index 扩展，再用 FastMCP Client 接入 MCP tools/resources/prompts；Capability Index 注入通过 `CapabilityChanged` / `ContextBuild` hooks 完成。
8. 实现 Memory Compact 四层压缩，先保证不切断 tool_use / tool_result 配对，再接入 `PreCompact` / `PostCompact` hooks。
9. 实现用户 command hooks，先支持同步本地命令、timeout、exit code 2 阻断和 JSON 输出解析。
10. 最后实现 SubAgent，同步子 Agent 先落地，再按 `subAgent.md` 增加 sidechain transcript、输出查询、取消、MCP / Skill 权限收窄和 `SubagentStart` / `SubagentStop` hooks。
