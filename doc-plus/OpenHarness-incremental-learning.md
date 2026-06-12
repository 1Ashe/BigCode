# OpenHarness 对照学习：BigCode 小步迭代路线

本文不是要求 BigCode 照搬 OpenHarness。BigCode 当前目标仍以 `doc/BigCode.md` 定义的 Python coding agent 核心闭环为准：Claude Messages 路线、上下文构建、工具调用、权限控制、Hooks、压缩、Plan/Task 和 SubAgent。OpenHarness 更像完整产品化 agent harness，值得学习的是工程边界、可观测性、恢复能力、配置体验和生态扩展，而不是一次性把 UI、频道、swarm、后台运行时全部搬进来。

## 1. 一句话结论

BigCode 是核心闭环内核，OpenHarness 是完整产品化 harness。BigCode 近期最应该补的是“稳定运行”和“可诊断”能力：启动前知道配置是否可用，运行中知道 agent loop 正在做什么，恢复后知道上次读过什么、加载过什么、验证过什么。

换句话说，BigCode 现在不缺方向，缺的是更产品化的运行护栏。优先级应该是 `doctor/dry-run`、provider workflow/profile、事件化 loop、session snapshot、tool output artifact 和测试矩阵；不要一上来抄 React/Textual TUI、多频道、swarm pane、复杂 plugin marketplace 或后台 worktree 编排。

## 2. 能力对照表

| 类别 | BigCode 当前设计/模块 | OpenHarness 可学习点 | BigCode 近期取舍 |
|------|------------------------|----------------------|------------------|
| Agent Loop | `doc/BigCode.md` 定义主链路；`bigcode/agent/session.py`、`bigcode/context/*`、`bigcode/tools/runner.py` 承担核心闭环。Claude Messages / tool_result 配对以 `doc/Context.md` 为准。 | `engine/query.py` 更像产品化 query engine：把 streaming、状态更新、工具事件、错误分层和 session 写入放进同一条可观测管线。 | 保留非 TUI 优先；先引入内部事件模型，让 CLI 日志、测试和后续 UI 都消费同一套事件。 |
| Provider/Auth | `doc/Config.md` 的 `models.json` 已要求 provider、`base_url`、`api_key_env`、capabilities、token 限制一起解析；现有模块有 `bigcode/config/models.py`、`bigcode/models/claude_compatible.py`、`bigcode/models/openai_compatible.py`。 | OpenHarness 的 provider 配置更接近 profile/workflow：用户能理解当前走哪个 provider、哪个鉴权、哪些能力可用，错误也更容易落到具体配置项。 | 继续优先 Claude-compatible；把“裸 provider 配置”升级为可解释 profile，不急着扩很多 provider。 |
| Context/Memory | `doc/Context.md` 已定义 `messages -> context_messages -> api_messages`、transcript/resume、tool_result budget；`doc/memory-compact-deep-dive.md` 定义压缩策略；现有模块有 `bigcode/context/transcript.py`、`compact.py`、`builder.py`。 | `services/session_storage.py` 一类 session 层把恢复、历史、active artifacts、最近状态做成明确存储边界。 | 加 session snapshot 和 metadata carryover：已读文件、已加载 Skill、最近验证动作、active artifacts 先持久化，提升 resume/compact 后连续性。 |
| Tool/Permission | `doc/Tool.md` 已有权限模式、路径安全、并发调度、输出预算、MCP/Skill/Agent 工具边界；现有模块有 `bigcode/tools/permissions.py`、`runner.py`、`output_limits.py`、`read_file_state.py`。 | OpenHarness 的工具执行更强调可诊断：每次工具开始、结束、失败、被拒绝都能被上层追踪。 | 增加 `ToolStarted` / `ToolCompleted` / `ErrorEvent`，并把大输出 artifact 化，避免上下文和日志同时膨胀。 |
| Skill/Plugin | `doc/MCP-Skill.md` 明确 Skill 是本地指令和资源能力，MCP 是 client 侧消费；现有模块有 `bigcode/skills/*`、`bigcode/mcp/*`。 | `plugins/loader.py` 的 manifest、加载边界、启停状态和错误汇总更成熟。 | 先做最小 manifest 兼容和加载诊断，不做 marketplace、远程 skill search、脚本型 Skill 执行。 |
| Task/SubAgent | `doc/subAgent.md` 已定义同步 subAgent、工具池收窄、sidechain transcript、后台 Agent Task 作为后续能力；现有模块有 `bigcode/subagents/*`、`bigcode/tasks/*`。 | `tasks/manager.py` 类任务管理能把 background agent 的状态、输出文件、取消和恢复做成产品能力。 | 先把同步 subAgent 和父子上下文边界跑稳；后台 task 放到 Phase 4，并要求有持久状态和输出查询。 |
| CLI/UX | `bigcode/cli.py`、`bigcode/__main__.py` 是入口；`doc/Config.md` 和 `doc/BigCode.md` 已有运行态边界，但启动前诊断仍薄。 | `cli.py` 值得学的是命令组织、doctor/dry-run、清晰错误和配置摘要，而不是立刻做完整 TUI。 | 新增 `/doctor` 或 `--dry-run`：预检模型、API key、MCP、Skill、工具注册和命令入口。 |
| Testing | `doc/Config.md`、`doc/Tool.md`、`doc/MCP-Skill.md`、`doc/subAgent.md` 都列了测试建议；当前已有 `tests/test_bigcode_core.py`。 | OpenHarness 更像按子系统组织测试：provider、session、plugin、task、tool event、CLI 都可单独验证。 | 拆分测试矩阵，避免所有核心回归都压在单个 core 测试文件里。 |

## 3. 近期最值得学的 6 件事

1. `dry-run/doctor`：新增启动前预检，覆盖模型引用、API key 环境变量、provider `base_url`、MCP server 配置、Skill 注册目录、工具 registry、命令入口和 workspace 权限。它应该输出“可运行 / 有风险 / 会失败”的分级结果，解决当前 BigCode 报错体验弱的问题。映射文档：`doc/Config.md`、`doc/Tool.md`、`doc/MCP-Skill.md`。

2. Provider profile：把 `models.json` 从“裸 provider 配置”升级成“可解释的 provider workflow/profile”。profile 至少说明 provider 类型、模型能力、鉴权来源、网络目标、Claude Messages 兼容路径和失败排查建议。BigCode 应继续保留 Claude-compatible 优先，只把 OpenHarness 的配置体验学过来。映射模块：`bigcode/config/models.py`、`bigcode/models/claude_compatible.py`。

3. 事件化 agent loop：引入内部 `StreamEvent`、`StatusEvent`、`ErrorEvent`、`ToolStarted`、`ToolCompleted`。第一阶段只服务 CLI 日志、单元测试和调试输出，不急着做 TUI。这样后续无论是 Textual、React 还是普通 JSONL trace，都不需要重写 agent loop。映射文档：`doc/BigCode.md`、`doc/Hooks.md`、`doc/Context.md`、`doc/Tool.md`。

4. Session snapshot + tool metadata carryover：在 transcript 之外保存 session snapshot，记录 session id、cwd、模型 profile、permission mode、已读文件摘要、已加载 Skill、MCP capability index、最近验证动作、active artifacts 和未完成 task/subAgent 状态。目标是让 resume 和 compact 后还能延续工作，而不是只恢复聊天文本。映射模块：`bigcode/context/transcript.py`、`bigcode/tools/read_file_state.py`、`bigcode/skills/loader.py`、`bigcode/tasks/store.py`。

5. Tool output artifact offload：大输出落盘，`tool_result` 只回传摘要、截断正文、路径和 metadata。`doc/Tool.md` 已定义执行侧输出上限，`doc/Context.md` 已定义 `tool_result_reference` attachment；下一步是把 artifact 存储路径、摘要格式、读取工具和清理策略做成稳定接口。映射模块：`bigcode/tools/output_limits.py`、`bigcode/context/builder.py`。

6. 更强测试矩阵：学习 OpenHarness 按子系统测试的组织方式，把 BigCode 测试拆成 provider/config、context/normalizer、tool/permission、mcp/skill、session/resume、cli/doctor、subAgent/task、artifact/offload 等文件。测试不需要一开始很大，但要能定位失败属于哪个边界。映射文档：`doc/Config.md`、`doc/Context.md`、`doc/Tool.md`、`doc/MCP-Skill.md`、`doc/subAgent.md`。

## 4. 中后期再学，不现在做

以下能力有价值，但现在引入会扩大依赖和复杂度，容易把 BigCode 从“核心闭环内核”拉成半成品平台。它们应该等配置、诊断、事件、session、artifact 和测试矩阵稳定后再分批评估。

| 能力 | 为什么有价值 | 为什么现在不做 |
|------|--------------|----------------|
| Plugin manifest 完整兼容 | 有利于生态包、版本声明、能力发现和启停管理。 | BigCode 还没有稳定的 doctor、加载错误聚合和最小 manifest；先做兼容子集即可。 |
| Background agent task | 能支持长任务、并行探索、输出查询和取消。 | `doc/subAgent.md` 已把它列为后续；同步 subAgent、sidechain transcript 和权限收窄先稳定。 |
| Sandbox-runtime | 能提升工具执行隔离、复现和安全边界。 | 当前更迫切的是权限模型、路径安全和诊断；完整 sandbox 会带来平台差异和调度成本。 |
| Memory auto-dream | 有助于长期记忆和跨 session 学习。 | `doc/Context.md` 已有 compact/resume 任务；先保证 snapshot 和显式 carryover，避免自动记忆污染上下文。 |
| React/Textual TUI | 能提升产品体验和状态可视化。 | 没有事件化 loop 和稳定状态模型时，TUI 会把业务状态写散。 |
| ohmo 多频道 | 能把状态、日志、工具、用户消息分通道表达。 | BigCode 目前 CLI 即可；先用内部事件模型保留扩展点。 |
| Swarm pane/worktree | 适合复杂并行研发和多 agent 编排。 | BigCode 当前重点是单 agent 和同步 subAgent 的可靠闭环；swarm 会显著提高任务管理和工作区隔离复杂度。 |

## 5. 分阶段路线图

| Phase | 目标 | BigCode 落点 | OpenHarness 参照 | 验收信号 |
|-------|------|--------------|------------------|----------|
| Phase 1：诊断与可观测性 | 新增 `/doctor` 或 `--dry-run`，完善模型/API 错误、配置预检、MCP/Skill 预览和工具注册摘要。 | `bigcode/cli.py`、`bigcode/config/loader.py`、`bigcode/config/models.py`、`bigcode/tools/registry.py`、`bigcode/mcp/client.py`、`bigcode/skills/loader.py`；对应 `doc/Config.md`、`doc/Tool.md`、`doc/MCP-Skill.md`。 | `cli.py` 的命令体验，`engine/query.py` 的状态/错误分层。 | 不发起真实长任务也能判断配置是否会失败；错误能指向 provider、API key、MCP server、Skill 或工具入口。 |
| Phase 2：运行连续性 | 新增 session snapshot、resume 列表、tool metadata carryover、大输出 artifact。 | `bigcode/context/transcript.py`、`bigcode/context/builder.py`、`bigcode/tools/output_limits.py`、`bigcode/tools/read_file_state.py`、`bigcode/tasks/store.py`；对应 `doc/Context.md`、`doc/Tool.md`、`doc/TaskPlan.md`。 | `services/session_storage.py` 的 session 边界和 artifact 思路。 | compact/resume 后仍知道已读文件、已加载能力、最近验证动作和大输出路径。 |
| Phase 3：生态接口 | 补 skill/plugin 兼容加载的最小 manifest、内置 skill 包、命令注册表。 | `bigcode/skills/models.py`、`bigcode/skills/loader.py`、`bigcode/hooks/command.py`、`bigcode/mcp/tools.py`；对应 `doc/MCP-Skill.md`、`doc/Hooks.md`。 | `plugins/loader.py` 的 manifest、加载状态和错误聚合。 | Skill/Plugin 加载失败不会破坏主循环；doctor 能列出启用、禁用、失败和原因。 |
| Phase 4：后台能力 | 补 background subAgent task、`TaskOutput`/`TaskStop`、`SubagentStart`/`SubagentStop` 持久状态。 | `bigcode/subagents/tool.py`、`bigcode/subagents/definitions.py`、`bigcode/tasks/models.py`、`bigcode/tasks/store.py`、`bigcode/hooks/bus.py`；对应 `doc/subAgent.md`、`doc/TaskPlan.md`、`doc/Hooks.md`。 | `tasks/manager.py` 的 task 状态、输出文件、取消和恢复。 | 后台 subAgent 可查询、可取消、可恢复输出；父 agent 不吞入完整子 agent 噪音。 |
| Phase 5：隔离和产品化 | 评估 sandbox、TUI、channel gateway、swarm。 | 需要在 Phase 1-4 的事件、session、task、artifact 边界稳定后再定模块；对应 `doc/BigCode.md` 的系统边界和各子系统权威文档。 | OpenHarness 的产品化 harness 形态。 | 只有当 CLI/事件/session 已稳定时，UI 和多 agent 编排才不会反向污染核心 loop。 |

这条路线的核心原则是：先让 BigCode 变得可诊断、可恢复、可测试，再扩展生态和后台能力。OpenHarness 的成熟度值得学习，但 BigCode 的优势是核心闭环清晰、依赖少、Claude Messages 路线明确；这些优势不应该被一次性功能对齐消耗掉。
