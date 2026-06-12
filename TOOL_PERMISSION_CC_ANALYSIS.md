# BigCode `tool_permission` 与 Claude Code 权限系统源码对照审计

> 审计对象：
>
> - BigCode：`/home/qt/BigCode`
> - Claude Code 参考实现（下文简称 CC）：`/home/qt/claude-code-rev`
> - 审计日期：2026-06-06

## 1. 结论摘要

BigCode 的权限系统不是独立设计出来的，它明确参考了 CC 的核心概念和执行形态：

- `default`、`acceptEdits`、`plan`、`bypassPermissions` 四种模式直接对应 CC。
- 工具通过 `check_permissions()` 返回 `allow`、`deny`、`ask` 或 `passthrough`，对应 CC 的 `checkPermissions()` 和 `PermissionResult`。
- 权限上下文包含 allow、deny、ask 三组规则。
- Plan Mode、非交互模式、子代理权限模式、PreToolUse hook 和工具执行前权限收敛，都能在 CC 找到直接对应关系。
- BigCode 文档直接列出了 CC 的源码路径，因此“参考 CC”不是推测。

但 BigCode 并不是 CC 权限系统的 Python 等价实现。它把 CC 分散在规则系统、各工具权限函数、Bash AST、安全检查、sandbox、交互 UI 和持久化层中的逻辑，压缩成了一个约 400 行的集中式分类器：

```text
PermissionTarget
  + permission_category
  + hard deny
  + 简单 fnmatch 规则
  + 四种 mode 默认策略
  + yes/no 终端询问
```

这个简化方向本身合理，代码也比 CC 容易阅读，但当前实现已经出现了安全边界不闭合的问题。最重要的结论如下：

1. **BigCode 文档描述的安全目标明显强于实际代码。**
2. **`acceptEdits` 会在权限层错误放行工作区外的 `Write` 和 `Edit`。**
3. **`Edit.call()` 没有工作区执行期兜底，工作区外文件存在实际修改路径。**
4. **`bypassPermissions` 可放行对工作区外路径产生副作用的 Bash 命令。**
5. **BigCode 的 `sandbox_profile` 只是权限过滤策略，不是 CC 那种进程级 sandbox。**
6. **显式 allow 和 hook approve 会跳过工具自己的 `check_permissions()`，与文档中的“工具约束不可绕过”不一致。**
7. **BigCode 复用了 CC 的子代理“父级宽权限优先”策略，却没有复用 CC 同等级的 Bash、路径和 sandbox 防护，因此相同策略在 BigCode 中风险更高。**
8. **现有相关测试全部通过，但测试没有覆盖上述关键路径。**

推荐先修复确定性的安全边界，再吸收 CC 的规则来源、结构化决策原因、权限更新和持久化设计。CC 的自动分类器、复杂 TUI 和大量实验模式不应成为第一阶段目标。

---

## 2. 审计范围与判断方法

本报告不仅阅读 BigCode 文档，还沿着实际运行链路检查了：

- 配置如何生成 `ToolPermissionContext`。
- 模型 tool use 如何进入 `ToolRunner`。
- hook 修改后的输入是否重新校验。
- `decide_permission()` 的真实优先级。
- 文件、Bash、Web、MCP、Skill 和 Agent 工具如何执行。
- 权限模式如何进入 Plan Mode、snapshot 和子代理。
- CC 中相同概念的真实实现位置。

重点源码如下。

### BigCode

- `bigcode/tools/base.py`
- `bigcode/tools/permissions.py`
- `bigcode/tools/runner.py`
- `bigcode/tools/paths.py`
- `bigcode/tools/read_tool.py`
- `bigcode/tools/write_tool.py`
- `bigcode/tools/edit_tool.py`
- `bigcode/tools/bash_tool.py`
- `bigcode/tools/web_fetch_tool.py`
- `bigcode/config/loader.py`
- `bigcode/agent/session.py`
- `bigcode/subagents/definitions.py`
- `bigcode/mcp/tools.py`
- `bigcode/skills/tools.py`
- `doc/Tool.md`
- `doc/Config.md`
- `doc/subAgent.md`

### CC

- `src/types/permissions.ts`
- `src/Tool.ts`
- `src/utils/permissions/permissions.ts`
- `src/utils/permissions/PermissionRule.ts`
- `src/utils/permissions/permissionRuleParser.ts`
- `src/utils/permissions/PermissionUpdate.ts`
- `src/utils/permissions/permissionsLoader.ts`
- `src/utils/permissions/filesystem.ts`
- `src/tools/BashTool/bashPermissions.ts`
- `src/services/tools/toolHooks.ts`
- `src/hooks/toolPermission/PermissionContext.ts`
- `src/components/permissions/`
- `src/utils/sandbox/sandbox-adapter.ts`
- `src/tools/AgentTool/runAgent.ts`

需要说明：`/home/qt/BigCode` 当前不是 Git 仓库，无法通过提交历史证明某段代码从 CC 的哪个版本复制而来。因此本报告所说的“参考”分为两类：

- **直接证据**：BigCode 文档明确写出 CC 源码路径。
- **结构证据**：类型名称、模式名称、决策行为和调用关系与 CC 高度同构。

---

## 3. BigCode 权限系统的实际实现

## 3.1 核心数据结构

BigCode 在 `bigcode/tools/permissions.py:21-58` 定义了三个核心对象。

### `PermissionMode`

```python
Literal["default", "acceptEdits", "plan", "bypassPermissions"]
```

这是 CC 外部权限模式的子集。CC 当前还包括：

- `dontAsk`
- 内部模式 `auto`
- 内部模式 `bubble`

BigCode v1 不实现这些模式是合理的，尤其 `auto` 依赖分类器、拒绝追踪和额外的安全策略，不适合在基础权限闭环尚未完成时加入。

### `PermissionRule`

BigCode 规则字段为：

```python
tool_name
behavior
pattern
source
reason
```

规则由所在列表决定真实行为：

- `always_deny`
- `always_ask`
- `always_allow`

虽然对象本身还有 `behavior` 字段，但 `_match_rules()` 不读取该字段。配置中如果把一条 `behavior="deny"` 的规则放入 `always_allow`，它仍会被当成 allow。这说明当前 `behavior` 字段与列表结构存在双重事实来源。

### `PermissionTarget`

BigCode 将工具输入压缩成通用权限目标：

```python
tool_name
category
path
command
network_url
raw
```

这个设计是 BigCode 相比 CC 最明显的自主简化。优点是所有工具可进入统一管线，缺点是信息损失明显：

- MCP 的 server、tool、resource URI 没有独立字段。
- Agent 的类型只能从 `raw` 临时读取。
- 文件权限目标只有一个未经统一规范化的 `Path`。
- Bash 没有子命令、重定向、工作目录变化和路径效果。
- 一个工具如果同时涉及多个路径，当前模型无法表达。

## 3.2 配置加载

`bigcode/config/loader.py:39-73` 按以下顺序读取配置：

1. BigCode home。
2. repo `.bigcode`。
3. cwd `.bigcode`。
4. CLI override。

随后 `_parse_permissions()` 将 `settings.permissions` 转为 `ToolPermissionContext`。

当前支持：

```json
{
  "permissions": {
    "mode": "default",
    "always_allow": [],
    "always_deny": [],
    "always_ask": [],
    "should_avoid_permission_prompts": false
  }
}
```

已实现的优点：

- 非法 mode 降级为 `default` 并记录错误，不会让整个程序无法进入 `/doctor`。
- 支持字符串形式的工具级规则。
- 支持字典形式的 `tool_name` 和 `pattern`。

当前缺失：

- 没有严格校验规则 behavior。
- 没有校验工具名是否存在。
- 没有规则语法 parser。
- 没有保留 user、project、local、policy、CLI、session 等真实来源。
- 合并完成后所有配置规则都标为 `source="config"`。
- 没有危险 allow 规则清洗。
- 没有管理策略锁定。
- 没有规则更新和持久化接口。

CC 的 `PermissionRuleSource` 明确区分：

```text
userSettings
projectSettings
localSettings
flagSettings
policySettings
cliArg
command
session
```

该来源不仅用于展示，还决定路径 pattern 的解析根目录、规则能否编辑、规则优先级和企业策略。

## 3.3 ToolRunner 调用链

BigCode 的实际执行链位于 `bigcode/tools/runner.py:100-214`：

```text
查找工具
  -> Pydantic schema 校验
  -> tool.is_enabled
  -> tool.validate_input
  -> PreToolUse hook
  -> hook updated_input 重新 schema 校验
  -> tool.validate_input 再执行
  -> decide_permission
  -> ask/deny
  -> tool.call
  -> 输出 artifact / 截断
  -> PostToolUse
```

这里有几项实现值得保留：

- hook 修改输入后重新执行 schema 校验。
- hook 修改输入后重新执行 `validate_input()`。
- hard deny 和 sandbox 位于 hook approve 之后，hook 不能直接绕过这些统一检查。
- 非交互模式不会阻塞等待 stdin。
- 权限检查与工具执行分离，Bash 工具本身不重复实现通用权限流程。

但当前 runner 没有使用权限决策里的 `updated_input`。`PermissionDecision.updated_input` 在类型中存在，CC 也依赖它传递规范化或用户修改后的输入，但 BigCode 最终仍调用原来的 `input_model`：

```python
perm = await decide_permission(...)
...
result = await tool.call(input_model, ctx)
```

这意味着未来即使工具权限检查或 PermissionRequest hook 返回安全改写后的输入，执行层也不会自动采用它。

## 3.4 `decide_permission()` 的真实顺序

`bigcode/tools/permissions.py:122-170` 的真实顺序是：

```text
1. build_permission_target
2. hard deny
3. always_deny
4. always_ask
5. always_allow
6. hook allow/ask/deny
7. tool.check_permissions
8. mode default
9. Bash/Plan tool constraints
10. sandbox profile
11. non-interactive ask -> deny
```

这个顺序与 `doc/Tool.md` 的设计描述存在一个关键差异。

文档声称：

> 显式 allow 不能覆盖工具约束。

但代码只在没有命中显式 allow、没有 hook 决策时才调用 `tool.check_permissions()`。因此：

- `always_allow` 会跳过工具自己的检查。
- hook approve 会跳过工具自己的检查。
- `apply_tool_constraints()` 只补充了少量 Bash 和 Plan Mode 逻辑，无法替代每个工具的安全检查。

CC 的当前实现则先执行工具 `checkPermissions()`，明确保留工具返回的 deny、内容级 ask 和 safety check，然后才处理 bypass 和整工具 allow。PreToolUse hook allow 也会再次调用 `checkRuleBasedPermissions()`，确保 deny、ask 和安全检查仍然生效，见：

- `src/utils/permissions/permissions.ts:1158-1318`
- `src/services/tools/toolHooks.ts:321-390`

## 3.5 四种模式的实际行为

### `default`

- 工作区内普通文件读取允许。
- 工作区外读取 ask。
- `skill` 类别直接允许。
- 被粗分类为只读的 Bash 允许。
- 所有 `state` 类工具允许。
- 写、编辑、删除、网络、Agent、MCP 通常 ask。

### `acceptEdits`

设计目标是只允许工作区内编辑，但通用 mode 代码为：

```python
if mode == "acceptEdits" and category in {"write", "edit"}:
    return allow
```

这里没有检查 `target.path` 是否位于 workspace。

`WriteTool.check_permissions()` 自己检查了 workspace，但由于外部路径返回 `passthrough` 后仍会进入通用 mode 默认值，所以最终仍是 allow。`Write.call()` 在执行时再次检查 workspace，因此写入被执行期拦截。

`EditTool` 没有覆盖 `check_permissions()`，`Edit.call()` 也没有检查 `inside_workspace`，所以风险不是单纯的错误提示，而是存在实际的工作区外修改路径。

### `plan`

Plan Mode 使用工具名 allowlist：

- 文件和搜索读取。
- Plan 文件读写。
- AskUserQuestion、ExitPlanMode。
- Task 只读工具。
- Skill 读取。
- MCP resource 和 prompt 读取。
- 粗分类为只读的 Bash。
- explorer 和 planAgent。

其余工具拒绝，并对 write、edit、delete 再做一次约束。

这种双层约束思路正确，但工具名 allowlist 容易随新增工具产生漂移。更稳妥的方式是同时声明：

- capability：read/write/network/process/state。
- effect：none/workspace/external。
- plan policy：allow/deny/custom。

### `bypassPermissions`

BigCode 并没有真正“绕过所有检查”：

- hard deny 仍执行。
- Bash danger/unknown 约束仍执行。
- sandbox profile 仍执行。

这个方向符合 BigCode 自己的安全目标。

问题是 hard deny 覆盖面不足，且 Bash 的 `mutate` 分类不会在 bypass 下收紧。于是下面这类命令会被允许：

```bash
touch /tmp/outside-workspace
rm -rf /tmp/some-directory
cp secret /outside/path
git push
```

只要没有命中非常有限的 `sudo`、`su` 或 `rm -rf /|~|$HOME` 正则，就会执行。

## 3.6 路径处理

BigCode 的 `resolve_path()` 做了正确的基础工作：

- 已存在路径使用 `resolve(strict=True)`。
- 新文件解析父目录后再拼文件名。
- workspace 判断使用真实路径。
- 记录 symlink escape。

但权限引擎没有统一使用这个结果。`build_permission_target()` 只构造原始 `Path`，规则匹配使用：

```python
str(target.path)
```

因此：

- allow/deny pattern 匹配的是用户输入，不是规范化路径。
- symlink 原路径和真实路径不会同时匹配规则。
- 相对路径 pattern 的根目录语义不明确。
- 工具执行层和权限层可能对同一路径得到不同结论。

CC 的文件权限会检查原始路径和 symlink 解析后的所有路径，并根据规则来源决定相对 pattern 的根目录，见 `src/utils/permissions/filesystem.ts:620-707`、`1030-1405`。

## 3.7 Bash

BigCode 的 Bash 分类器按以下方式工作：

- 正则发现复杂 shell 操作符时返回 `unknown`。
- 使用 `shlex.split()` 取首个可执行文件。
- 对少数 `git`、`find`、`sed` 子命令特殊处理。
- 其余按 read/mutate 集合判断。

优点：

- 无法证明安全时不会误标为 read。
- `sed -i`、`find -delete` 等常见变更操作能识别。
- Plan Mode 不允许 unknown 或 mutate。

不足：

- 不解析多个子命令。
- 不解析输出重定向目标。
- 不分析 `cd` 后的工作目录。
- 不分析命令参数中的文件路径。
- 不分析 wrapper、环境变量和解释器代码。
- `mutate` 只是要求普通模式询问，在 bypass 下直接放行。
- 执行使用普通 `asyncio.create_subprocess_shell()`，没有 OS 级隔离。

CC 当前已使用 tree-sitter Bash AST 或 legacy parser，分别处理：

- 子命令权限结果合并。
- malformed syntax。
- command injection。
- 输出重定向路径。
- `cd` 和工作目录。
- exact/prefix/wildcard 规则。
- sandbox 自动允许。

参考 `src/tools/BashTool/bashPermissions.ts:1663-2385`。

BigCode 不需要完整复制这两千多行逻辑，但不能把简单分类器描述成与 CC 等价的 Bash 安全层。

## 3.8 Web 和 SSRF

BigCode WebFetch 已实现：

- 只允许 HTTP/HTTPS。
- 拒绝显式 localhost 和 metadata hostname。
- 拒绝直接写出的私网、回环、链路本地和保留 IP。
- 每次重定向重新校验 URL。

主要缺口是 hostname 不会先解析到 IP 再校验。对于普通域名，函数在 `ipaddress.ip_address(host)` 失败后直接返回。因此恶意域名或 DNS rebinding 可以解析到私网地址。

正确实现应在建立连接前：

1. 解析全部 A/AAAA 地址。
2. 拒绝任一不安全地址。
3. 将校验结果与实际连接地址绑定，避免检查和连接之间发生 DNS 变化。
4. 对每次重定向重复执行。

## 3.9 MCP

BigCode 的 MCP resource/prompt 工具统一声明 `permission_category="mcp"`，这是正确的接入方向。

但 `PermissionTarget` 没有 MCP server、tool、URI 或 transport 字段。规则 pattern 的匹配字符串也只从 command、path、URL 中选取，因此像 `ExternalResourceRead(server="x", uri="y")` 这样的调用没有可供 pattern 匹配的目标内容。

结果是当前权限只能粗粒度控制：

```text
允许/拒绝整个 ExternalResourceRead 工具
```

而不能表达：

```text
允许某个 MCP server
允许某个 server 的某个 tool
只允许 resource read
禁止 stdio 启动本地进程
按 URL/domain 限制 HTTP transport
```

BigCode `doc/MCP-Skill.md` 中描述的 server/tool 级权限目标尚未在 `permissions.py` 落地。

## 3.10 Skill

Skill 是 BigCode 当前实现得相对扎实的部分：

- 只能按 registry 名称加载。
- resource path 拒绝绝对路径和 `..`。
- 使用 `resolve(strict=True)` 后再次确认位于 skill root。
- 拒绝敏感 basename。

这里与 BigCode 的“Skill 不能成为任意文件读取旁路”目标基本一致。

仍可改进：

- `SkillLoad` 的 SKILL.md 读取也应确认注册时保存的真实路径仍位于合法 root。
- 敏感文件规则应统一使用权限层的路径分类，不应只共享一个 basename 集合。
- Skill 内容应带明确的不可信来源元数据，防止模型把其中指令误当作系统级规则。

## 3.11 子代理

BigCode 子代理会：

- 创建独立 `AgentSession`。
- 使用非交互模式。
- 复制权限规则。
- 复用 workspace、task、plan、MCP、Skill 和 hook。
- clone `ReadFileState`。
- 按 agent definition 裁剪工具。

权限模式解析为：

```python
if parent_mode in {"bypassPermissions", "acceptEdits"}:
    return parent_mode
return definition.permission_mode or parent_mode
```

这与 CC 当前 `runAgent.ts:412-450` 的思路一致：父级 bypass 或 acceptEdits 优先于 agent 定义。

问题是 BigCode 的 explorer/code-reviewer/planAgent 工具列表中保留了 Bash。父级如果是 bypass，子代理会从 `plan` 变成 bypass，Bash 的 mutating 命令就可能执行。

CC 采用相似继承策略，但它同时具备：

- 更细的 agent 工具池过滤。
- Bash 子命令和路径权限。
- PermissionRequest bubbling。
- 可用的 OS sandbox。
- 内容级 deny/ask 和 safety check。

因此不能只复制继承规则而忽略配套约束。对 BigCode 当前阶段，更安全的策略是：

- agent definition 标记为只读时，`plan` 必须优先于父级宽模式。
- 只读 agent 的 Bash 即使保留，也必须固定使用 plan Bash 策略。
- 后台 agent 无法询问时，ask 必须拒绝或显式 bubble 给父会话。

---

## 4. BigCode 具体参考了 CC 的哪些设计

| BigCode 设计 | CC 对应设计 | 判断 |
| --- | --- | --- |
| 四种核心权限模式 | `PermissionMode` | 直接参考 |
| `allow/deny/ask/passthrough` | `PermissionResult` | 直接参考 |
| `ToolPermissionContext` | 同名上下文 | 直接参考 |
| allow/deny/ask 三组规则 | `alwaysAllowRules` 等 | 直接参考 |
| 工具级 `check_permissions()` | `Tool.checkPermissions()` | 直接参考 |
| 非交互 ask 转 deny | `shouldAvoidPermissionPrompts` | 直接参考 |
| Plan Mode 保存并恢复旧 mode | `prePlanMode` | 直接参考 |
| 子代理 definition 可指定 mode | AgentDefinition permission mode | 直接参考 |
| 父级 bypass/acceptEdits 优先 | CC `runAgent.ts` | 直接参考 |
| PreToolUse 可改写输入和给权限结果 | CC tool hooks | 核心形态参考 |
| 权限检查在工具调用前统一执行 | CC tool execution | 核心形态参考 |
| 工具注册时要求权限声明 | CC Tool 协议思想 | 简化实现 |

BigCode 文档也直接写明参考路径，例如 `doc/Tool.md:61-70` 指向：

- CC `src/Tool.ts`
- CC `src/services/tools/toolExecution.ts`
- CC `src/utils/permissions/`
- CC `src/tools/BashTool/`
- CC 文件读写工具
- CC Plan Mode 工具

---

## 5. BigCode 没有参考或只参考了表面的部分

## 5.1 规则来源和配置治理

CC 的规则按来源保存，BigCode 将来源压平为 `config`。缺失的能力包括：

- 企业 policy 不可被项目配置覆盖。
- CLI 和 session 规则独立。
- 相对路径按设置文件目录解析。
- UI 能解释规则来自哪里。
- 只删除可编辑来源的规则。
- managed-only 模式。

建议参考，不需要照搬 CC 的全部 settings 基础设施。

## 5.2 PermissionUpdate

CC 权限询问不只是返回布尔值，还能产生结构化更新：

- add/replace/remove rules。
- set mode。
- add/remove working directories。
- destination 为 session、user、project、local 等。

BigCode 当前用户批准一次后不会改变上下文，下次相同操作还会询问。

这是最值得吸收的 CC 设计之一，因为它同时改善：

- 用户体验。
- 非交互自动化。
- 规则可审计性。
- 权限 UI 与权限内核的解耦。

## 5.3 结构化决策原因

CC 的 `PermissionDecisionReason` 可区分：

- rule
- mode
- subcommandResults
- hook
- sandboxOverride
- classifier
- workingDir
- safetyCheck
- other

BigCode 目前主要使用字符串 `reason` 和 `rule`。这不利于：

- UI 精确展示。
- 测试断言。
- 审计事件。
- 对 ask 进行不同的非交互处理。
- 区分可被 bypass 的普通 ask 和不可 bypass 的 safety ask。

建议参考。

## 5.4 PermissionRequest hooks

BigCode 有 PreToolUse/PostToolUse，但没有独立的 PermissionRequest 和 PermissionDenied 闭环。

CC 对 headless/async agent 会先执行 PermissionRequest hook，hook 可：

- allow。
- deny。
- 修改输入。
- 返回权限更新。
- 决定是否中断。

BigCode 非交互模式直接把 ask 转 deny，无法通过受控外部审批系统继续运行。

建议在 PermissionUpdate 和结构化原因完成后加入。

## 5.5 专用审批 UI

CC 为不同工具提供不同审批组件：

- Bash。
- 文件写入和编辑。
- WebFetch。
- Skill。
- Plan Mode。
- 通用 fallback。

BigCode 目前只有单行摘要和 yes/no。对于 v1，这种简化可以保留，但至少应提供统一的选择模型：

```text
允许一次
本会话允许
项目允许
用户级允许
拒绝
拒绝并反馈
```

无需先复制 React TUI。

## 5.6 真实 sandbox

CC 会把权限和 sandbox 配置转换为 sandbox runtime 配置，并实际包装 Bash 命令。Linux 下会检查 bubblewrap 等依赖；sandbox 有文件读写和网络边界。

BigCode `apply_sandbox_profile()` 只是决定 allow/deny，`BashTool.call()` 仍直接调用系统 shell。

因此 BigCode 当前配置名容易造成错误安全预期。二选一：

1. 实现真实进程 sandbox。
2. 在实现前将其明确命名和描述为 `execution_policy_profile`，不要宣称已隔离进程。

## 5.7 Bash 规则和 AST

BigCode 只借鉴了“Bash 需要专门分类”这个概念，没有借鉴 CC 的真正权限模型：

- `Bash(command)` 规则 parser。
- exact/prefix/wildcard。
- 每个子命令独立结果。
- redirect path。
- command injection。
- wrapper 和环境变量。
- sandbox 路径。

建议吸收确定性解析和路径约束，不建议立刻复制分类器和实验特性。

## 5.8 模式治理

BigCode 未实现：

- bypass 是否可用的独立开关。
- bypass 首次启用警告。
- policy killswitch。
- 危险 allow rule 清洗。
- workspace trust 与 tool permission 分离。

其中 bypass 警告、可用性开关和 workspace trust 值得优先参考。

---

## 6. 安全与正确性问题

## P0-1：`Edit` 可修改工作区外文件

### 证据

- `apply_mode_default()` 在 acceptEdits 下按 category 放行，不检查 workspace。
- bypass 直接放行。
- `EditTool.call()` 解析路径后只检查 `is_file()`，没有检查 `inside_workspace`。

### 可达条件

- bypass 模式下先读取或已经有目标文件快照。
- acceptEdits 下外部读取被用户批准或显式 allow，随后 Edit 自动放行。

### 影响

模型可以修改 BigCode workspace 之外的用户文件。

### 修复

- `Edit.call()` 必须像 `Write.call()` 一样检查 `resolved.inside_workspace`。
- 权限层的 acceptEdits 必须要求规范化目标位于 allowed workspace。
- 外部目录只能通过明确的 additional working directory 更新加入，而不是单次读取批准后隐式获得编辑权。

## P0-2：Bash 可绕过 workspace 边界

### 证据

- `BashTool.call()` 使用普通 `create_subprocess_shell()`。
- `bypassPermissions` 默认 allow。
- `apply_tool_constraints()` 只拒绝 danger，并将 unknown 从 allow 降为 ask；mutate 保持 allow。
- hard deny 不分析一般命令参数路径。

### 影响

Bash 可以读写、删除、上传 workspace 外的数据。

### 修复

- 在真实 sandbox 完成前，不应把 bypass 与“安全自动执行”绑定。
- bypass 启用必须要求明确危险确认。
- 只读/工作区 profile 必须真正包装进程。
- Bash parser 必须检查重定向、`cd` 和常见路径参数。

## P0-3：allow 和 hook approve 跳过工具安全检查

### 证据

`decide_permission()` 只有在未命中 allow 且没有 hook 决策时才调用 `tool.check_permissions()`。

### 影响

当前 Write 有执行期兜底，但未来任何只在 `check_permissions()` 中实现关键约束的工具都可能被规则或 hook 绕过。

### 修复

将工具检查改为必经步骤，并将结果分成：

- hard deny。
- bypass-immune safety ask。
- ordinary ask/allow/passthrough。

显式 allow 和 hook allow 只能覆盖 ordinary ask，不能覆盖前两类。

## P0-4：只读子代理可能因父级宽模式获得写能力

### 证据

- 父级 acceptEdits/bypass 优先于 definition mode。
- explorer 等只读 agent 保留 Bash。
- bypass 下 mutate Bash 可执行。

### 修复

在 BigCode 当前防护能力下：

- `permission_mode="plan"` 的内置 agent 必须保持 plan。
- 或为只读 agent 注入不可覆盖的 capability ceiling。
- capability ceiling 应独立于 mode，父级只能进一步收紧，不能放宽。

## P1-1：权限层和执行层对 Write 的结论不一致

权限层允许工作区外 Write，执行层拒绝。虽然最终没有写入，但会：

- 产生误导审计记录。
- hook 和 UI 认为操作已授权。
- 让测试只检查 decision 时得到错误结论。

应在权限层直接 ask/deny。

## P1-2：规则可被原始路径和 symlink 表达绕过

规则匹配使用原始输入，没有同时检查真实路径。应统一构造：

```python
PermissionPathTarget(
    requested,
    absolute,
    resolved,
    parent_resolved,
    inside_workspace,
    is_symlink_escape,
)
```

deny 和 ask 规则应对所有相关路径执行，任一命中即生效。

## P1-3：规则 schema 存在歧义

`PermissionRule.behavior` 与所在列表可能冲突，而 `_match_rules()` 忽略 behavior。

修复方案：

- 上下文只存一组 `rules: list[PermissionRule]`；或
- 删除对象内 behavior，只由三个集合表达。

若参考 CC，推荐前者，便于来源、解释和更新。

## P1-4：敏感路径 hard deny 覆盖不足

当前主要按 basename 判断，容易漏掉：

- `.env.local`
- shell profile
- `.git/config`、hooks
- `.claude/settings.json`
- 云配置目录中的非 `credentials` 文件
- Windows 特殊路径

建议把敏感路径分成：

- 永久 deny。
- 必须人工确认的 safety ask。
- acceptEdits 不可自动批准。

不要把所有敏感文件都永久 deny，否则用户无法完成合法维护操作。

## P1-5：SSRF 域名解析缺口

显式私网 IP 会拒绝，但解析到私网的域名不会。应增加 DNS/IP 绑定校验。

## P1-6：MCP 权限粒度不足

当前无法按 server/tool/transport/resource 控制。应扩展 `PermissionTarget`，并对 stdio 和 HTTP 分别应用 process/network 约束。

## P1-7：用户批准无法形成规则

yes/no 只能批准本次调用。建议引入 PermissionUpdate，先实现 session destination，再实现持久化 destination。

## P2-1：权限审计事件不足

BigCode 会发 ToolStarted、ToolCompleted、ErrorEvent，但权限拒绝只是普通工具错误。建议增加：

- PermissionEvaluated
- PermissionRequested
- PermissionApproved
- PermissionDenied
- PermissionRuleUpdated

事件中记录结构化 reason，不记录凭据或完整敏感输入。

## P2-2：`updated_input` 没有进入执行

权限决策支持 updated input，但 runner 忽略它。修复时必须重新用工具 schema 校验，并重新构造权限目标，防止改写后绕过检查。

---

## 7. 文档与实现偏差

## 7.1 `acceptEdits`

`doc/Tool.md:275-278` 声称 acceptEdits 只允许 workspace 内编辑，代码没有在 mode 默认策略中落实。

## 7.2 bypass 外部写入

`doc/Tool.md:286-293` 将 bypass 下外部写入和删除列为 hard deny，代码只禁止系统目录和极少数 broad rm 模式。

## 7.3 子代理不能提升权限

文档声称子代理不能提升权限，但实现会让声明为 plan 的子代理继承父级 acceptEdits/bypass。这个行为与 CC 相似，却与 BigCode 自己的安全描述冲突。

## 7.4 sandbox

文档使用 sandbox 一词描述隔离效果，实际只有权限决策过滤，没有进程隔离。

## 7.5 “风险已闭环”

`doc/Tool.md:779-802` 声称所有识别风险都有防护和测试。根据本次探针，该结论目前不成立。

现有测试通过只能证明已写出的行为未回归，不能证明文档列出的边界已实现。

---

## 8. 已执行验证

执行了：

```bash
python -m unittest \
  tests.test_bigcode_core \
  tests.test_sandbox_profiles \
  tests.test_background_subagents
```

结果：

```text
Ran 49 tests
OK
```

另外使用只读临时目录探针直接调用 `decide_permission()`，结果为：

| 模式 | 工作区外 Edit | 工作区外 Write | `touch` 外部路径 | `rm -rf` 外部目录 |
| --- | --- | --- | --- | --- |
| default | ask | ask | ask | ask |
| acceptEdits | allow | allow | ask | ask |
| bypassPermissions | allow | allow | allow | allow |
| plan | deny | deny | deny | deny |

这里 Write 最终会被 `Write.call()` 的 workspace 检查拒绝；Edit 没有同等检查。

应新增至少以下回归测试：

1. acceptEdits 的工作区外 Write 决策必须不是 allow。
2. acceptEdits 的工作区外 Edit 决策必须不是 allow。
3. Edit 执行期必须拒绝工作区外文件。
4. symlink 指向外部时 Edit/Write 拒绝。
5. explicit allow 不得覆盖工具 safety deny/ask。
6. hook approve 不得覆盖工具 safety deny/ask。
7. bypass 下外部 Bash 路径按明确策略处理。
8. plan 型子代理在父级 bypass 下仍不可执行 mutate Bash。
9. deny/ask pattern 同时匹配原始路径和真实路径。
10. 域名解析为私网地址时 WebFetch 拒绝。
11. MCP 规则能区分 server 和 tool。
12. permission updated input 会重新校验并用于执行。

---

## 9. 推荐的目标架构

不建议直接移植 CC 的全部实现。BigCode 应保留集中式、可读的 Python 架构，但补齐以下接口。

## 9.1 结构化规则

```python
PermissionRuleSource = Literal[
    "user",
    "project",
    "local",
    "policy",
    "cli",
    "session",
]

@dataclass(frozen=True)
class PermissionRuleValue:
    tool_name: str
    content: str | None = None

@dataclass(frozen=True)
class PermissionRule:
    source: PermissionRuleSource
    behavior: Literal["allow", "deny", "ask"]
    value: PermissionRuleValue
```

规则字符串可以兼容 CC 的：

```text
Read(src/**)
Edit(src/**)
Bash(git status)
Bash(npm run:*)
Agent(explorer)
mcp__server__tool
```

解析器必须支持转义、规范化和严格校验。

## 9.2 结构化目标

```python
@dataclass(frozen=True)
class PermissionTarget:
    tool_name: str
    category: PermissionCategory
    paths: tuple[ResolvedPermissionPath, ...] = ()
    command: CommandPermissionTarget | None = None
    network: NetworkPermissionTarget | None = None
    mcp: McpPermissionTarget | None = None
    agent: AgentPermissionTarget | None = None
```

每个工具通过 `prepare_permission_target()` 提供目标，通用 fallback 只能用于无副作用工具。不能再依赖从任意 Pydantic 字段里猜 `path`、`command`、`url`。

## 9.3 结构化决策原因

```python
PermissionDecisionReasonType = Literal[
    "hard_deny",
    "safety_check",
    "rule",
    "tool_constraint",
    "mode",
    "hook",
    "sandbox",
    "non_interactive",
    "working_directory",
]
```

`PermissionDecision` 应包含：

- behavior。
- reason。
- updated_input。
- suggestions。
- blocked target。
- 是否可被 bypass。
- 是否必须用户交互。

## 9.4 PermissionUpdate

第一版先支持：

```python
AddRules
RemoveRules
SetMode
AddWorkspaceRoot
RemoveWorkspaceRoot
```

destination 第一阶段只实现 `session`，第二阶段再支持 user/project/local 持久化。

## 9.5 决策顺序

推荐 BigCode 使用以下固定流程：

```text
0. schema + validate_input
1. PreToolUse 聚合和输入改写
2. 改写后重新 schema + validate_input
3. 构造规范化 PermissionTarget
4. hard deny
5. 整工具 explicit deny
6. tool.check_permissions / safety checks（必经）
7. 内容级 deny 和 bypass-immune safety ask
8. explicit ask
9. hook deny/ask
10. bypass mode
11. explicit allow
12. hook allow
13. mode default
14. OS sandbox 可执行性检查
15. 非交互 PermissionRequest hook 或 ask -> deny
16. 用户审批和 PermissionUpdate
17. updated input 重新校验、重新构造 target、只允许收窄后执行
```

关键不变量：

- hard deny 永远优先。
- 工具安全检查永远执行。
- allow 和 hook approve 不能覆盖 safety check。
- 输入发生任何变化后都重新判断权限。
- sandbox 是执行边界，不是一个普通 allow/deny 标签。

---

## 10. 分阶段优化路线

## 第一阶段：关闭现有安全缺口

目标是不新增复杂功能，只让当前文档描述与代码一致。

改动：

- acceptEdits 只允许规范化后的 workspace 内 edit/write。
- Edit 增加执行期 workspace 检查。
- 显式 allow 和 hook approve 仍执行工具安全检查。
- 引入只读 agent capability ceiling。
- 修复规则 behavior 歧义。
- 权限目标统一使用 `resolve_path()`。
- 将 sandbox profile 文档改称策略过滤，或实现真实 sandbox 后再恢复名称。
- bypass 增加显式危险确认和可用性开关。

验收：

- 本报告 P0 测试全部通过。
- 所有 allow 都有明确的规范化目标。
- 决策层和执行层对 workspace 结论一致。

## 第二阶段：规则和解释能力

改动：

- 引入结构化 rule source/value。
- 实现 `Tool(content)` parser。
- 文件规则按来源 root 解析。
- MCP server/tool 规则。
- Agent type 规则。
- 结构化 decision reason。
- 权限诊断命令列出有效、冲突和不可达规则。

验收：

- deny > ask > allow 的冲突测试。
- symlink、相对路径和多来源规则测试。
- 配置错误不会静默变成宽权限。

## 第三阶段：审批闭环

改动：

- PermissionUpdate。
- 允许一次/会话/项目/用户级审批。
- PermissionRequest 和 PermissionDenied hooks。
- 非交互调用可由可信 hook 审批。
- 权限更新写入 session snapshot。
- 增加权限审计事件。

验收：

- 相同调用在 session allow 后不重复询问。
- 持久化失败不应错误地更新内存状态。
- policy rule 不可被 UI 删除或覆盖。
- headless ask 不阻塞。

## 第四阶段：Bash 与真实 sandbox

改动：

- 使用 tree-sitter-bash 或等价 AST。
- 分析子命令、redirect、`cd`、wrapper 和路径。
- Linux 使用 bubblewrap 或成熟 sandbox runtime。
- 网络访问按域名和解析后 IP 限制。
- sandbox 配置从 permission rules 派生。
- sandbox 不可用时 fail closed 或明确降级并禁止自动放行。

验收：

- Bash 无法写 workspace 外路径。
- 重定向、命令替换和复合命令不能绕过。
- sandbox 中网络和文件限制由实际进程测试验证。

## 第五阶段：高级自动化

只有前四阶段稳定后再考虑：

- `dontAsk`。
- `bubble`。
- AI classifier/auto mode。
- permission explainer。
- denial tracking。

这些能力提升体验，但不能替代确定性的路径、规则和 sandbox。

---

## 11. 哪些 CC 设计不应直接照搬

### 不应直接复制完整 `permissions.ts`

CC 当前权限系统承载大量产品实验、遥测、feature flag 和兼容逻辑。直接移植会破坏 BigCode “清晰、可测试”的目标。

### 不应优先实现 auto classifier

分类器只能决定普通 ask，不能修复路径解析、规则优先级和 sandbox 缺失。

### 不应先复制 React 权限组件

BigCode 当前 CLI 可先用统一选择模型实现 PermissionUpdate，UI 可以后置。

### 不应把所有 CC safety ask 改成 BigCode hard deny

永久 deny 虽然简单，但会阻止合法维护。应区分：

- 永久禁止。
- 必须人工确认。
- 可由 session 规则放行。

### 不应继续复制“父级宽模式优先”而忽略 capability ceiling

CC 的策略依赖更完整的执行防护。BigCode 当前应先确保只读 agent 真正只读。

---

## 12. 最终评价

BigCode 已经复现了 CC 权限系统的骨架，但还没有复现其安全闭环。

目前最有价值的部分是：

- 集中式权限入口。
- 明确的 permission category。
- fail-closed 的非交互策略。
- Plan Mode 二次限制。
- 路径解析工具。
- Web 重定向重复检查。
- Skill resource 防穿透。

目前最危险的误区是：

> 把“有 hard deny、四种 mode 和 sandbox profile”理解成已经具备了 CC 同等级的权限边界。

实际上 CC 的安全性来自多层组合：

```text
规则来源
+ 工具专属权限
+ 文件真实路径
+ Bash 子命令和重定向
+ PermissionRequest
+ 权限更新
+ workspace trust
+ OS sandbox
+ 专用交互
```

BigCode 只实现了其中一部分。正确的优化方向不是把 CC 全量搬过来，而是先补齐确定性边界，再逐步加入规则来源、PermissionUpdate、结构化原因和真实 sandbox。

在完成第一阶段修复和对应测试前，建议：

- 不将 `bypassPermissions` 用于不受控环境。
- 不将 `sandbox_profile` 视为实际进程隔离。
- 不允许只读子代理继承 bypass 后执行 Bash。
- 删除或修正文档中“风险已经闭环”的结论。
