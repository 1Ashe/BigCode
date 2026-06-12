# Memory Compact 四级压缩机制详解

## 概述

本文是 `Context.md` 中 BigCode 目标模块 `bigcode/context/compact.py` 的深度设计文档，只负责解释长上下文压缩算法。消息类型、tool_use / tool_result 配对、API normalizer、transcript、resume 和 `PreCompact` / `PostCompact` lifecycle hooks 的事件语义仍以 `Context.md` 与 `Hooks.md` 为准。

BigCode 的 context window 管理采用**四种压缩策略级联**架构，按触发阈值和 agent loop 时机执行。核心设计思想：用最小代价释放 context 空间，同时保护当前任务的关键上下文。

```
utilization:  0% ────── 50% ────── 70% ────── 75% ────── 85% ────── 95%
                      Micro       Snip      Collapse    Auto       Blocked
                      compact     Compact              Compact
```

执行顺序参考 Claude Code 的 agent loop 行为，BigCode v1 在主 Agent Loop 中按同样层次接入：

1. **Snip Compact** — 每个 turn 一次（不是每个 step）
2. **Microcompact** — 每个 step 都执行
3. **Context Collapse** — 每个 step 都执行
4. **Auto-Compact** — 只在 step 0，且 warningLevel 为 critical/blocked 时执行

---

## 候选区间查找：Snip 和 Collapse 共用的逻辑

在讲每一层之前，先讲清楚"候选区间"是怎么确定的。这是 Snip 和 Collapse 都要用到的核心逻辑。

### 假设场景

你在用 BigCode 帮你改一个 React 项目，当前消息数组（25 条，约 10,500 tokens）：

```
index  role                   content                               tokens
─────  ─────                  ───────                               ──────
[0]    system                 "You are a coding assistant..."       100
[1]    user                   "帮我重构 src/auth.ts"                50
[2]    assistant              "好的，我先看看..."                    100
[3]    assistant_tool_call    read_file({path: "src/auth.ts"})      30
[4]    tool_result            "export function login() { ... }"     2000
[5]    assistant              "我看到了，现在来改..."                80
[6]    assistant_tool_call    edit_file({path: "src/auth.ts"})      20
[7]    tool_result            "File updated successfully"           10
[8]    user                   "测试跑一下看看"                      20
[9]    assistant_tool_call    run_command({cmd: "npm test"})        30
[10]   tool_result            "FAIL: JWT expired"                   3000
[11]   assistant              "有个错误，JWT 过期，我来修..."        150
[12]   assistant_tool_call    edit_file({path: "src/auth.ts"})      20
[13]   tool_result            "File updated successfully"           10
[14]   user                   "再跑一次测试"                        15
[15]   assistant_tool_call    run_command({cmd: "npm test"})        30
[16]   tool_result            "All 42 tests passed"                 1500
[17]   assistant              "全部通过了"                          50
[18]   user                   "好，接下来帮我加个 /api/users 接口"   30
[19]   assistant              "好的，我先看看项目结构..."            100
[20]   assistant_tool_call    list_files({path: "src/"})            20
[21]   tool_result            "src/auth.ts\nsrc/routes.ts..."       500
[22]   assistant_tool_call    read_file({path: "src/routes.ts"})    30
[23]   tool_result            "import { Router }..."                3000
[24]   assistant              "我看到路由结构了，开始写接口..."      100
```

### Step 1: 确定保护区

两层都做同一件事：保护最近 12 条消息 + 最后一条 user 消息之后的所有内容。

```typescript
keepRecentStart = messages.length - 12 = 25 - 12 = 13
lastUserIndex = 18  // "好，接下来帮我加个 /api/users 接口"
end = min(13, 18) = 13
```

**index >= 13 的消息全部是保护区，不碰。**

为什么要找最后一条 user 消息？因为 user 消息之后的所有内容（19~24）都是**正在执行的"加 /api/users 接口"这个任务**。如果删了，模型就忘了自己正在干什么。

### Step 2: 确定候选区间起点

从头扫描 boundary 消息（system / context_summary / snip_boundary），跳过它们：

```typescript
let start = 0
for (let i = 0; i < end; i++) {
  if (isBoundaryMessage(messages[i]!)) {
    start = i + 1
  }
}
// messages[0] 是 system → start = 1
```

**候选区间 = [1, 13)**，即 index 1~12 共 12 条消息。

如果之前已经做过一次 snip，数组里会有 snip_boundary，start 会跳到它之后，避免重复处理。

---

## Microcompact（50% 阈值）—— 轻量工具输出清理

> Claude Code 参考：`/home/qt/claude-code-rev/src/services/compact/microCompact.ts`
> 阈值：`MICROCOMPACT_UTILIZATION = 0.50`

### 核心思路

最保守的压缩。只清理旧的工具输出文本，不改变消息结构，不调用 LLM。

### 触发条件

`utilization >= 50%`

### 执行逻辑

1. 扫描整个消息数组，找到所有属于"可清理工具"的 `tool_result`。可清理工具：`read_file`、`run_command`、`search_files`、`list_files`、`web_fetch`
2. 保留最近 3 个 tool_result（`KEEP_RECENT_TOOL_RESULTS = 3`），其余替换为 `[Output cleared for context space]`

### 例子

```
可清理的 tool_result（按出现顺序）：

index [4]  read_file   "export function login() { ... }"     2000 tokens
index [10] run_command  "FAIL: JWT expired"                   3000 tokens
index [16] run_command  "All 42 tests passed"                 1500 tokens
index [21] list_files   "src/auth.ts\nsrc/routes.ts..."       500 tokens
index [23] read_file    "import { Router }..."                3000 tokens

保留最后 3 个（index 16、21、23），清理前 2 个（index 4、10）：

index [4]  → "[Output cleared for context space]"   释放 ~2000 tokens
index [10] → "[Output cleared for context space]"   释放 ~3000 tokens
```

edit_file 的 tool_result（index 7、13）不在可清理列表里，所以不会被清理。

### 设计取舍

- 零 LLM 成本，执行极快
- 清理后 cache 前缀不命中（content 变了），但保留最近 3 个不动，最新的工具结果不受影响
- 本质是**用 cache 命中率换 context 空间**

---

## Snip Compact（70% 阈值）—— 确定性中段裁剪

> Claude Code 参考：`/home/qt/claude-code-rev/src/services/compact/snipCompact.ts`
> 阈值：`SNIP_COMPACT_THRESHOLD = 0.70`
> 目标：`SNIP_TARGET_USAGE = 0.60`

### 核心思路

确定性地删除对话中段的一段连续消息。不调用 LLM，通过规则保护重要操作。**物理删除，不可恢复。**

### 触发条件

`utilization >= 70%`

### 完整执行流程

#### Step 1: 分组

把候选区间 [1, 13) 内的消息分成 group。`tool_call` + 它对应的 `tool_result` 算一个 group：

```
G0: [1]      user "帮我重构..."              50 tokens   unprotected
G1: [2]      assistant "好的，我先看看..."    100 tokens  unprotected
G2: [3]+[4]  read_file call + result         2030 tokens unprotected
G3: [5]      assistant "我看到了，现在来改..." 80 tokens   unprotected
G4: [6]+[7]  edit_file call + result         30 tokens   ← 包含 edit_file
G5: [8]      user "测试跑一下看看"            20 tokens   unprotected
G6: [9]+[10] run_command + FAIL result       3030 tokens ← 包含 error
G7: [11]     assistant "有个错误，JWT 过期..." 150 tokens  unprotected
G8: [12]     assistant_tool_call edit_file    20 tokens   ← 没有配对的 tool_result
```

注意 G8：index 12 是 `assistant_tool_call(edit_file)`，但它的 tool_result 在 index 13，而 index 13 在候选区间 end=13 **之外**（保护区）。所以 G8 只有 tool_call 没有 tool_result → `protected: true, reason: unclosed_tool_call`。

#### Step 2: 标记保护

三类保护规则，且保护有**传染性**（邻居也保护）：

**规则一：文件编辑工具**

```typescript
PROTECTED_TOOL_NAMES = {edit_file, modify_file, patch_file, write_file, apply_patch}
```

包含这些工具的 group → protected，并且**前后各 1 个邻居 group 也 protected**（`protectNearbyGroups`，snipCompact.ts:204-208）。

```
G4 包含 edit_file → G4 protected
  → protectNearbyGroups(groups, 4) → G3(前邻居)、G4、G5(后邻居) 全部 protected

G8 包含 edit_file → G8 protected
  → protectNearbyGroups(groups, 8) → G7(前邻居)、G8 全部 protected（G9 不存在）
```

**规则二：错误信息**

```typescript
ERROR_MARKERS = ['error', 'failed', 'failure', 'exception', 'traceback', 'permission denied']
```

tool_result 的 content 包含这些关键词 → protected，邻居也保护。

```
G6 的 tool_result 包含 "FAIL" → G6 protected
  → protectNearbyGroups(groups, 6) → G5(前邻居)、G6、G7(后邻居) 全部 protected
```

**最终保护状态：**

```
G0 [1]    user "帮我重构..."              ❌ unprotected
G1 [2]    assistant "好的..."             ❌ unprotected
G2 [3-4]  read_file call + result         ❌ unprotected
G3 [5]    assistant "我看到了..."          ✅ protected (near_file_edit，G4的邻居)
G4 [6-7]  edit_file call + result         ✅ protected (is_file_edit)
G5 [8]    user "测试跑一下"               ✅ protected (near_file_edit + near_error)
G6 [9-10] run_command + FAIL result       ✅ protected (is_error)
G7 [11]   assistant "有个错误..."          ✅ protected (near_file_edit + near_error)
G8 [12]   edit_file call (unclosed)       ✅ protected (unclosed + is_file_edit)
```

#### Step 3: 找最大连续 unprotected 段

把连续的 unprotected group 构成 safe run：

```
safeRun = [G0, G1, G2] = index [1, 5)，约 2180 tokens
```

只有这一段。其余 group 全部被保护。

#### Step 4: 过滤 + 选择删除范围

```typescript
// 过滤条件（snipCompact.ts:431-434）
safeRuns.filter(run =>
  run.messagesCount >= SNIP_MIN_MESSAGES_TO_REMOVE &&  // >= 6 条消息
  run.tokens >= SNIP_MIN_TOKENS_TO_FREE                 // >= 2000 tokens
)
```

G0+G1+G2 共 4 条消息 [1,2,3,4]，messagesCount = 4 < 6 → **不满足最小删除条数 → 这次 snip 不执行。**

如果 safe run 足够大（比如有 8 条消息 5000 tokens），则从开头累加 group，直到满足 desiredTokensToFree 且 messagesCount >= 6，然后删除那个范围，插入 snip_boundary。

#### Step 5: 如果 snip 成功执行了

删除 [start, end) 的消息，插入一条 `snip_boundary`：

```
删除前: [0] [1] [2] [3] [4] [5] [6] [7] [8] [9] [10] [11] [12] [13] ... [24]

删除后: [0] [snip_boundary] [5] [6] [7] [8] [9] [10] [11] [12] [13] ... [24]
              ↑
    "[Snipped earlier conversation segment]
     Removed range: messages: 4, approximate tokens freed: ~2180"
```

被删除的消息**物理移除**，session log 里也没有了。

---

## Context Collapse（75% 阈值）—— 投影层摘要

> Claude Code 参考：`/home/qt/claude-code-rev/src/services/compact/snipProjection.ts`、`/home/qt/claude-code-rev/src/services/compact/reactiveCompact.ts`、`/home/qt/claude-code-rev/src/services/compact/compact.ts`
> 阈值：`CONTEXT_COLLAPSE_UTILIZATION = 0.75`
> 目标：`CONTEXT_COLLAPSE_TARGET_USAGE = 0.65`

### 核心思路

最精巧的一层。用 LLM 对中段消息生成摘要，但**原文保留在 session log 中**，模型看到的是投影后的摘要视图。

### 与 Snip 的本质区别

```
Snip:     物理删除 + 插 boundary 标记 → 不可逆，零 LLM 成本
Collapse: LLM 生成摘要 + 投影替换     → 可逆（原文还在），需要一次 LLM 调用
```

### 触发条件

`utilization >= 75%`（基于投影后的 utilization 计算）

### 完整执行流程

#### Step 1: 分组（与 Snip 不同！）

Collapse 的 `buildMessageGroups`（252-326行）只检查：
- 未配对的 tool_call → protected
- boundary 消息 → protected

**不检查** edit_file、error 信息。这意味着 Collapse 比 Snip 激进得多。

```
G0:  [0]      system                     100   protected (boundary)
G1:  [1]      user "帮我重构..."          50    unprotected
G2:  [2]      assistant "好的..."         100   unprotected
G3:  [3]+[4]  read_file call + result     2030  unprotected
G4:  [5]      assistant "我看到了..."      80    unprotected
G5:  [6]+[7]  edit_file call + result     30    unprotected  ← Collapse 不保护 edit_file！
G6:  [8]      user "测试跑一下"           20    unprotected
G7:  [9]+[10] run_command + FAIL result   3030  unprotected  ← Collapse 不保护 error！
G8:  [11]     assistant "有个错误..."      150   unprotected
G9:  [12]+[13] edit_file call + result    30    end=14 > protectedStart=13 → protected
G10: [14]     user "再跑一次"             15    end=15 > 13 → protected
G11~G17: ...                             全部 protected (在保护区内)
```

**关键差异**：G5（edit_file）和 G7（FAIL）在 Collapse 里是 unprotected！Collapse 有 LLM 兜底，所以可以更激进。

#### Step 2: 找 safe runs

遍历所有 group，排除 protected 的：

```
safeRun = [G1, G2, G3, G4, G5, G6, G7, G8]
        = index [1, 12)，约 8490 tokens
```

G9 开始在保护区内（end > protectedStart），所以 safe run 到 G8 为止。

#### Step 3: 从 safe run 构建 candidate

`buildCandidateFromGroups`（380-426行）从 safe run 的第一个 group 开始累加，估算摘要大小（`max(128, tokens * 0.15)`），直到节省的 token >= desiredTokensToSave：

```
desired = max(2000, currentTokens - effectiveInput * 0.65)
        = max(2000, 10500 - 10400) = 2000

累加过程：
i=0: G1(50),    累计=50,    摘要≈128, 节省=0       < 2000 继续
i=1: G2(100),   累计=150,   摘要≈128, 节省=22      < 2000 继续
i=2: G3(2030),  累计=2180,  摘要≈327, 节省=1853    < 2000 继续
i=3: G4(80),    累计=2260,  摘要≈339, 节省=1921    < 2000 继续
i=4: G5(30),    累计=2290,  摘要≈344, 节省=1946    < 2000 继续
i=5: G6(20),    累计=2310,  摘要≈347, 节省=1963    < 2000 继续
i=6: G7(3030),  累计=5340,  摘要≈801, 节省=4539    >= 2000 break!
```

candidate = index [1, 11)，即 G1~G7。

摘要大小估算公式（`estimateCollapseSummaryTokens`，129行）：`Math.max(128, Math.ceil(tokensBefore * 0.15))` — 原文的 15%，至少 128 tokens。

#### Step 4: 调用 LLM 生成摘要

把 candidate 中的消息转成文本，喂给 LLM（`buildContextCollapseSummaryPrompt`，517行）：

```
Prompt: "You are creating a local context-collapse summary...
Preserve:
- User intent and active goals
- Tool calls and tool results that still matter
- File reads/writes and code changes, with paths, function names
- Errors, failures, warnings, and exact messages when relevant
..."

Messages to summarize:
[User]: 帮我重构 src/auth.ts，加上 JWT 验证
[Assistant]: 好的，我先看看当前代码...
[Tool Call: read_file src/auth.ts]: {"path":"src/auth.ts"}
[Tool Result: read_file src/auth.ts]: export function login() { ... }
[Assistant]: 我看到了，现在来改...
[Tool Call: edit_file src/auth.ts]: {"path":"src/auth.ts",...}
[Tool Result: edit_file src/auth.ts]: File updated successfully
[User]: 测试跑一下看看
[Tool Call: run_command npm test]: {"cmd":"npm test"}
[Tool Result: run_command npm test ERROR]: FAIL: JWT expired
[Assistant]: 有个错误，JWT 过期，我来修...
```

LLM 返回摘要（`<summary>` 标签内）：
```
用户要求重构 src/auth.ts 添加 JWT 验证。读取原始代码后执行 edit_file。
测试失败（JWT expired），assistant 识别了错误原因。
```

#### Step 5: 验证 + 提交

实际摘要约 200 tokens，原文 5340 tokens，节省 5140 > MIN_TOKENS_TO_SAVE(2000) → 通过。

创建 CollapseSpan，状态 committed。每次 pass 最多处理 2 个 span（`MAX_SPANS_PER_PASS = 2`）。

#### Step 6: 投影（projectCollapsedView）

**原始 session log（不变）：**
```
[0]  system
[1]  user: "帮我重构..."
[2]  assistant: "好的..."
[3-4] read_file auth.ts
[5]  assistant: "我看到了..."
[6-7] edit_file auth.ts
[8]  user: "测试跑一下"
[9-10] run_command npm test (FAIL)
[11] assistant: "有个错误..."
[12-13] edit_file auth.ts
[14] user: "再跑一次测试"
...后面不变...
```

**模型看到的投影：**
```
[0]  system
[1]  context_summary: "[Collapsed context summary]
       用户要求重构 src/auth.ts 添加 JWT 验证。读取原始代码后
       执行 edit_file。测试失败（JWT expired），识别了错误原因。"
[14] user: "再跑一次测试"
...后面不变...
```

index 14 之后的消息，providerUsage 被标记为 stale（上下文变了，cache 前缀不匹配了）。

### 第二次 Collapse 的行为

当 utilization 再次 >= 75%，`findCollapseCandidate` 再次被调用。此时 `collapsedIds` 包含第一次折叠的 message ID，这些 group 会被跳过，**同一段消息不会被折叠两次**。

---

## Auto-Compact（85% 阈值）—— LLM 全量压缩

> Claude Code 参考：`/home/qt/claude-code-rev/src/services/compact/compact.ts` + `/home/qt/claude-code-rev/src/services/compact/autoCompact.ts`
> 阈值：`AUTOCOMPACT_UTILIZATION = 0.85`

### 核心思路

最后的兜底。保留最近 40k token / 6 条消息，其余全部交给 LLM 做结构化摘要。**物理替换，原文不可恢复。**

### 与 Context Collapse 的关键区别

| | Context Collapse | Auto-Compact |
|--|--|--|
| 范围 | 中段一小段（最多 2 个 span） | 除了最近 40k token 之外的**全部** |
| 原文 | 保留在 session log | **不可恢复** |
| 触发频率 | 每个 step 都可以 | 只在 step 0 |
| 阈值 | 75% | 85% |
| LLM 调用 | 摘要一小段 | 摘要整个旧对话 |

### 触发条件

`step === 0`（agent loop 第一步）且 `warningLevel === 'critical' || 'blocked'`

### 完整执行流程

#### Step 1: 确定保留边界

`findRetentionBoundary`（compact.ts:58-86）从尾部往前扫描，累加 token：

```
[24] assistant: 100     (累计 100)
[23] tool_result: 3000  (累计 3100)
[22] tool_call: 30      (累计 3150)
[21] tool_result: 500   (累计 3650)
[20] tool_call: 20      (累计 3700)
[19] assistant: 100     (累计 3800)
[18] user: 30           (累计 3830)
[17] assistant: 50      (累计 3880)
[16] tool_result: 1500  (累计 5380)
...继续往前扫...
```

扫到 MAX_KEEP_TOKENS(40000) 或至少保留 MIN_KEEP_MESSAGES(6) 条时停止。

假设 boundary 落在 index 8（"测试跑一下看看"）。然后 `alignBoundaryToApiRound` 确保 boundary 不切断一个 tool_call + tool_result 对。

#### Step 2: 转文本 + 调用 LLM

boundary 之前的消息转文本（`messagesToText`，88-122行），tool_result 超过 500 字符会被截断：

```
[User]: 帮我重构 src/auth.ts，加上 JWT 验证
[Assistant]: 好的，我先看看当前代码...
[Tool Call: read_file]: {"path":"src/auth.ts"}
[Tool Result: read_file]: export function login() { ... }... (truncated)
[Tool Call: edit_file]: {"path":"src/auth.ts",...}
[Tool Result: edit_file]: File updated successfully
```

喂给 LLM 的 summary prompt 要求输出结构化摘要，包含 section：Primary Request、Key Decisions、Files Modified、Errors Encountered、Current State、Pending Tasks。

#### Step 3: 替换消息

```typescript
newMessages = [
  ...systemMessages,     // system 保留
  summaryMessage,        // 一条 context_summary 替代所有旧消息
  ...messagesToKeep,     // boundary 之后的消息保留
]
```

```
替换前: [0] [1] [2] [3] [4] [5] [6] [7] [8] [9] ... [24]
         sys ─────── 被压缩的区间 ────────  ── 保留 ──

替换后: [0] [context_summary] [8] [9] [10] ... [24]
         sys    结构化摘要       ───── 保留 ─────
```

被替换的消息**物理删除**，不可恢复。

#### Step 4: 重置 Collapse 状态

Auto-Compact 执行后会重置 Context Collapse 状态（agent-loop.ts:230）：

```typescript
replaceContextCollapseState(createContextCollapseState())
```

因为全量压缩后之前的 span 引用全部失效了。

### 缓存问题

`compact.ts:163`：`const response = await modelAdapter.next(summaryRequestMessages)`

这是一个**全新的 LLM 请求**（system + summary prompt），和原始对话的 prompt cache 完全不共享。没有 fork 机制。每次 auto-compact 都是独立的、额外的 LLM 调用。

### 状态管理

- 连续失败 3 次（`MAX_AUTOCOMPACT_FAILURES = 3`）→ 永久禁用
- 手动 `/compact` 命令在 BigCode v1 可作为 `bigcode/commands/compact.py` 后置实现；Claude Code 的压缩核心参考 `/home/qt/claude-code-rev/src/services/compact/compact.ts`
- context window < 20000 tokens（`MIN_EFFECTIVE_INPUT_FOR_AUTOCOMPACT`）→ 不触发

---

## 完整执行流程图

```
agent-loop step 开始
  │
  ├─ HookBus.emit(PreCompact)
  │   └─ 保存 compact 前状态、记录 utilization；不改变候选区间算法
  │
  ├─ [Snip Compact] 每个 turn 一次
  │   └─ utilization < 70%? → 跳过
  │   └─ 找候选区间 → 分组 → 标记保护(edit+error+邻居)
  │     → 找最大连续 unprotected 段 → 物理删除 → 插入 snip_boundary
  │
  ├─ [Microcompact] 每个 step
  │   └─ utilization < 50%? → 跳过
  │   └─ 保留最近 3 个 tool_result，其余清空
  │
  ├─ [Context Collapse] 每个 step
  │   └─ utilization < 75%? → 跳过
  │   └─ 找候选（排除已折叠 ID，不保护 edit/error）
  │     → LLM 摘要 → 投影替换（原文保留）
  │
  ├─ [Auto-Compact] 仅 step 0，仅 critical/blocked
  │   └─ utilization < 85%? → 跳过
  │   └─ 保留最近 40k token → LLM 全量摘要 → 物理替换
  │     → 重置 Collapse 状态
  │
  ├─ HookBus.emit(PostCompact)
  │   └─ 记录 compact 结果，恢复 instruction/capability 等后续 ContextBuild 所需状态
  │
  └─ model.next(modelMessages) → 生成回复
```

Hooks 只处理 compact 生命周期副作用：

- `PreCompact`：记录 compact 前 utilization、保存需要恢复的动态提醒状态。
- `PostCompact`：记录 compact 结果、恢复 instruction / capability / plan 等后续 `ContextBuild` hooks 需要的状态。
- Hooks 不参与 Micro / Snip / Collapse / Auto 的候选区间、保护规则、阈值和摘要算法决策。

---

## 阈值总览

| 策略 | 触发阈值 | 执行时机 | 目标 | 最小操作 | LLM 调用 | 可逆性 |
|------|---------|----------|------|---------|---------|--------|
| Microcompact | 50% | 每个 step | - | 清理所有旧 tool_result | 无 | N/A |
| Snip Compact | 70% | 每个 turn 一次 | 60% | 6 条消息 / 2000 tokens | 无 | 不可逆 |
| Context Collapse | 75% | 每个 step | 65% | 2000 tokens | 有（摘要小段） | 可逆 |
| Auto-Compact | 85% | step 0 | - | - | 有（摘要全部） | 不可逆 |
| Blocked | 95% | 压缩后检查 | - | 拒绝新请求 | - | - |

## 关键文件

| 文件 | 职责 |
|------|------|
| `/home/qt/claude-code-rev/src/services/compact/microCompact.ts` | L2: 工具输出清理 |
| `/home/qt/claude-code-rev/src/services/compact/snipCompact.ts` | L1: 确定性中段裁剪 |
| `/home/qt/claude-code-rev/src/services/compact/grouping.ts` | Snip / Collapse 共用的消息分组逻辑 |
| `/home/qt/claude-code-rev/src/services/compact/snipProjection.ts` | Collapse 投影和 span 处理参考 |
| `/home/qt/claude-code-rev/src/services/compact/reactiveCompact.ts` | 接近阈值时的响应式压缩调度参考 |
| `/home/qt/claude-code-rev/src/services/compact/compact.ts` | L4: 全量 LLM 压缩核心逻辑 |
| `/home/qt/claude-code-rev/src/services/compact/autoCompact.ts` | L4: 自动触发和失败状态管理 |
| `/home/qt/claude-code-rev/src/services/compact/prompt.ts` | 摘要 prompt 模板 |
| `/home/qt/claude-code-rev/src/query/` | 主查询循环和压缩接入点参考 |
| `bigcode/context/compact.py` | BigCode v1 目标模块，集中实现四层压缩门面 |
