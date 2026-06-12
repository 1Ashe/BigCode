# BigCode 配置文件设计

本文档是 BigCode 配置系统的权威来源。其他文档可以说明各自子系统会消费哪些配置，但配置目录、加载顺序、合并规则、模型注册表和运行态目录边界以本文档为准。

## 目标和边界

配置系统负责把用户级、项目级和当前目录局部配置合并成一次会话的 `RuntimeConfig`。它不负责执行工具、不负责写 transcript，也不能绕过 `Tool.md` 定义的权限顺序和 hard deny。

v1 目标：

- 支持全局配置目录、项目配置目录和 cwd-local 配置目录。
- 汇总 MCP、Skill、Hooks、subAgent、Instruction、权限、workspace、模型和 Plan/Task 的配置入口。
- 允许用户在全局 Skill 库和项目 Skill 库之间覆盖同名 skill。
- 允许不同 provider / `base_url` 的模型同时存在；切换模型时必须同步切换对应 `base_url`、鉴权和能力声明。
- 区分用户手写配置和 BigCode 运行态产物，避免用户误改 transcript、task store 或 tool result。

非目标：

- 不定义 Tool Runner、Context normalizer 或 Agent Loop 的运行细节。
- 不把凭据明文注入模型上下文、transcript 或错误日志。
- 不允许配置扩大 workspace、绕过路径安全、关闭 hard deny 或提升 subAgent 权限。

## 配置目录

BigCode v1 使用三类配置目录：

```txt
~/.bigcode/                 # 用户级全局配置
<repo>/.bigcode/            # 项目级配置
<cwd>/.bigcode/             # 当前目录局部配置
/home/qt/.agents/skills/    # 兼容的额外全局 Skill 库
```

`<repo>` 是当前 workspace 的仓库根目录。`<cwd>` 是当前会话启动目录；如果 `<cwd>` 与 `<repo>` 相同，二者的 `.bigcode` 只加载一次。实现必须基于真实路径去重，不能用字符串前缀判断。

推荐用户手写文件布局：

```txt
.bigcode/
  settings.json
  models.json
  mcp.json
  instructions.md
  rules/
    *.md
  agents/
    *.md
  skills/
    <skill-name>/
      SKILL.md
      ...
```

运行态产物仍可放在 `.bigcode/` 或 `~/.bigcode/` 下，但不属于用户手写配置：

```txt
.bigcode/
  tasks/
  plans/
  tool-results/

~/.bigcode/
  projects/
    <project-id>/
      subagents/
        <agent-id>.jsonl
```

运行态目录由对应模块管理。配置加载器可以识别并跳过这些目录，但不解析其中内容作为配置。

## 加载和合并

默认加载优先级：

```txt
built-in < user < global-compat < project < cwd-local < env < cli < session override
```

含义：

- `built-in`：代码内置默认配置、内置 subAgent、内置 hooks。
- `user`：`~/.bigcode/`。
- `global-compat`：目前只用于 `/home/qt/.agents/skills/`。
- `project`：`<repo>/.bigcode/`。
- `cwd-local`：`<cwd>/.bigcode/`。
- `env`：环境变量，例如默认模型或 task list id。
- `cli`：命令行参数。
- `session override`：会话中工具或调用参数显式传入的覆盖值，例如 `AgentTool.model`。

合并规则：

- 字典按 key 递归合并。
- 同名 provider、model、MCP server、skill、agent 后加载覆盖先加载。
- 列表默认整体替换；只有字段明确声明为追加合并时才追加。
- `null` 表示显式清空该字段；缺失表示继承较低优先级配置。
- 配置文件 JSON 解析失败时记录配置错误，并跳过该文件；不能让一个坏文件导致所有配置丢失。
- 单个配置项校验失败时跳过该项并保留其他合法项；安全相关非法配置必须按 hard deny 或 explicit deny 处理。

## settings.json

`settings.json` 是通用配置入口，存放会话默认值、权限规则、workspace roots、hooks、Plan/Task 选项和小型 BigCode 子系统配置。

示例：

```json
{
  "default_model": "openai:gpt-5.2",
  "workspace_roots": [
    "/home/qt/extra-workspace"
  ],
  "permissions": {
    "mode": "default",
    "always_allow": [],
    "always_ask": [],
    "always_deny": [],
    "should_avoid_permission_prompts": false
  },
  "system_prompt": {
    "mode": "default",
    "content": null
  },
  "plan": {
    "default_dir": ".bigcode/plans"
  },
  "tasks": {
    "default_task_list_id": null
  },
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
    ]
  },
  "bigcode": {
    "mcp": {
      "enabled": true,
      "startup": "lazy",
      "default_timeout_seconds": 30,
      "expose_resources": true,
      "expose_prompts": true
    },
    "skills": {
      "enabled": true,
      "max_skill_md_chars": 20000
    }
  }
}
```

字段说明：

- `default_model`：主会话默认模型引用，必须能在模型注册表中解析。环境变量或 CLI 可以覆盖。
- `workspace_roots`：额外授权 root。启动时必须全部 `resolve(strict=True)`；不存在时启动失败或要求用户重配。
- `permissions`：映射到 `ToolPermissionContext`。规则不能覆盖 hard deny。
- `system_prompt`：映射到 `Context.md` 的 `SystemPromptMode`；`mode` 只允许 `default`、`append`、`replace`。
- `plan.default_dir`：默认计划目录，必须限制在 workspace 或 BigCode home 下。
- `tasks.default_task_list_id`：默认 task list id；非交互模式可被 `BIGCODE_TASK_LIST_ID` 覆盖。
- `hooks`：沿用 `Hooks.md` 用户 command hooks 结构。
- `bigcode.mcp` 和 `bigcode.skills`：子系统默认开关和预算；更详细的 MCP server 配置仍放在 `mcp.json`。

## models.json

模型配置必须使用注册表。BigCode 不允许只切换一个裸 `model` 字符串后由实现猜测 `base_url`。

推荐结构：

```json
{
  "default_model": "openai:gpt-5.2",
  "providers": {
    "openai": {
      "type": "openai-compatible",
      "base_url": "https://api.openai.com/v1",
      "api_key_env": "OPENAI_API_KEY",
      "default_headers": {},
      "models": {
        "gpt-5.2": {
          "id": "gpt-5.2",
          "capabilities": {
            "supports_images": true,
            "supports_tools": true,
            "supports_parallel_tool_calls": true,
            "supports_thinking": true
          },
          "context_window": 400000,
          "max_output_tokens": 32000
        }
      }
    },
    "local": {
      "type": "openai-compatible",
      "base_url": "http://127.0.0.1:8000/v1",
      "api_key_env": "LOCAL_LLM_API_KEY",
      "models": {
        "qwen-coder": {
          "id": "qwen3-coder",
          "capabilities": {
            "supports_images": false,
            "supports_tools": true,
            "supports_parallel_tool_calls": false,
            "supports_thinking": false
          },
          "context_window": 128000,
          "max_output_tokens": 8192
        }
      }
    }
  }
}
```

模型引用格式：

```txt
<provider-name>:<model-key>
```

例如 `openai:gpt-5.2` 解析为：

- provider：`providers.openai`
- 请求 `base_url`：`https://api.openai.com/v1`
- 鉴权环境变量：`OPENAI_API_KEY`
- provider 内模型 key：`gpt-5.2`
- API 请求 model id：`gpt-5.2`
- 模型能力：`capabilities`

切换规则：

- 主会话默认模型来自 `models.json.default_model` 或 `settings.json.default_model`，后者优先级按配置加载顺序合并。
- CLI / 环境变量可以覆盖默认模型，但覆盖值仍必须是模型注册表引用。
- subAgent frontmatter 的 `model` 字段、`AgentTool.model` 调用参数和主会话默认模型都引用同一个模型注册表。
- `model: inherit` 解析为 `None`，表示继承父 agent 当前已解析的模型配置。
- `AgentTool.model` 优先级高于 agent definition 的 `model`；二者都不能绕过注册表。
- 当模型引用切换到另一个 provider 时，模型适配器必须同时切换 `base_url`、`api_key_env`、headers、模型能力和 token 限制。
- 如果 provider 的 `base_url` 指向 localhost、内网或其他受限网络目标，是否允许连接由网络权限和 SSRF 策略决定；模型配置本身不能绕过权限。

校验规则：

- provider name 和 model key 推荐匹配 `^[a-zA-Z0-9_-]{1,64}$`。
- `type` v1 先支持 `openai-compatible`；后续可扩展 `anthropic`、`gemini` 等适配器。
- `base_url` 必须是 `http` 或 `https` URL，末尾是否带 `/v1` 由 provider 配置明确指定。
- `api_key_env` 只能是环境变量名，不能在配置文件中存明文 token。
- `default_headers` 不得进入模型上下文、transcript 或普通错误输出。
- capabilities 缺失时使用保守默认：不支持图片、不支持 thinking、不支持 parallel tool calls，只支持基础文本和工具能力由适配器确认。

## mcp.json

MCP server 配置沿用 FastMCP 兼容的 `mcpServers` 结构：

```json
{
  "mcpServers": {
    "weather": {
      "transport": "http",
      "url": "https://weather.example.com/mcp"
    },
    "local_assistant": {
      "transport": "stdio",
      "command": "python",
      "args": ["./assistant_server.py"],
      "cwd": "/path/to/server",
      "env": {
        "DEBUG": "true"
      }
    }
  },
  "bigcode": {
    "mcp": {
      "enabled": true,
      "startup": "lazy",
      "default_timeout_seconds": 30,
      "expose_resources": true,
      "expose_prompts": true
    }
  }
}
```

加载顺序：

```txt
~/.bigcode/mcp.json
<repo>/.bigcode/mcp.json
<cwd>/.bigcode/mcp.json
```

规则：

- 同名 server 后加载覆盖先加载。
- server name 只能匹配 `^[a-zA-Z0-9_-]{1,64}$`。
- `enabled=false` 的 server 不连接、不发现、不进入 capability index。
- `stdio` server 启动属于本地进程执行面，必须经过权限引擎。
- `http/sse` server 连接属于网络访问，必须经过网络权限和 SSRF 检查。
- `env`、headers、token 不进入模型上下文和 transcript 明文。

## Skills

Skill 注册目录按以下顺序扫描：

```txt
~/.bigcode/skills/*/SKILL.md
/home/qt/.agents/skills/*/SKILL.md
<repo>/.bigcode/skills/*/SKILL.md
<cwd>/.bigcode/skills/*/SKILL.md
```

规则：

- 后加载的同名 skill 覆盖先加载的 skill。
- `/home/qt/.agents/skills` 是兼容的全局 Skill 库，优先级高于 `~/.bigcode/skills`，低于项目和 cwd-local。
- skill name 默认取目录名，也可从 `SKILL.md` frontmatter 的 `name` 读取。
- name 必须匹配 `^[a-z0-9][a-z0-9_-]{0,63}$`。
- `SKILL.md` 和资源文件必须位于 skill 根目录真实路径下。
- symlink 指向 skill 根目录外时拒绝加载。
- Skill 不能修改 permission mode、不能新增 allow rule、不能扩大 workspace。

## Agents

subAgent markdown 定义目录按以下顺序扫描：

```txt
~/.bigcode/agents/*.md
<repo>/.bigcode/agents/*.md
<cwd>/.bigcode/agents/*.md
```

同名 agent 后加载覆盖先加载。frontmatter 字段以 `subAgent.md` 的 `AgentDefinition` 为准：

```yaml
---
name: code-reviewer
description: Review code changes for bugs, risks, and missing tests.
tools: Read, Grep, Glob, Bash
model: inherit
permission_mode: plan
max_turns: 8
background: false
---
```

模型字段必须按本文档 `models.json` 解析：

- `model: inherit` 表示继承父 agent 当前模型。
- 其他值必须是 `<provider-name>:<model-key>`。
- 找不到模型引用时该 agent 仍可加载，但启动时必须给出清晰错误，不能静默回退到另一个 provider。

## Instructions

Instruction files 发现规则以 `Context.md` 为准，配置系统只负责提供路径和加载顺序：

```txt
~/.bigcode/instructions.md
<repo>/BIGCODE.md
<repo>/.bigcode/instructions.md
<repo>/.bigcode/rules/*.md
<cwd>/BIGCODE.md
<cwd>/.bigcode/instructions.md
<cwd>/.bigcode/rules/*.md
<cwd>/BIGCODE.local.md
```

加载规则：

- 按 user -> project -> local 顺序渲染。
- 越靠近 `cwd` 的项目目录越晚加载。
- `BIGCODE.local.md` 是局部覆盖或补充，不建议提交到团队共享配置。
- `@include`、截断预算和 prompt 渲染仍由 Context 系统实现。

## Hooks

用户 command hooks 放在 `.bigcode/settings.json.hooks`。事件名、matcher、command hook 输入输出和聚合规则以 `Hooks.md` 为准。

配置系统只做：

- 合并多层 `hooks` 配置。
- 校验事件名和 hook 类型。
- 保留命令字符串和 timeout。
- 不执行 hook，不解释 hook 输出。

v1 不开放用户配置 subAgent 专属 hooks。

## Permissions 和 Workspace

权限配置映射到 `ToolPermissionContext`：

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

规则：

- `mode` 只允许 `default`、`acceptEdits`、`plan`、`bypassPermissions`。
- `always_allow` 不能覆盖 hard deny、工具约束、Plan Mode 或非交互归一化。
- `always_deny` 和 `always_ask` 优先于 `always_allow`。
- 规则语法 v1 可以先保持最小结构，后续再扩展 `tool(pattern)`。
- 非交互环境中最终 `ask` 必须转为 `deny`，除非有合法显式 allow。

workspace roots：

```json
{
  "workspace_roots": [
    "/home/qt/project-a",
    "/home/qt/project-b"
  ]
}
```

规则：

- 所有 root 启动时必须 `resolve(strict=True)`。
- 去重后只保留真实路径。
- 不存在的 root 不能静默忽略。
- workspace 配置不能扩大 subAgent 的父级 workspace；subAgent 只能继承或收窄。

## Plan 和 Task

Plan / Task 的业务状态以 `TaskPlan.md` 为准。配置系统只提供默认入口：

```json
{
  "plan": {
    "default_dir": ".bigcode/plans"
  },
  "tasks": {
    "default_task_list_id": null
  }
}
```

规则：

- `plan.default_dir` 必须位于 workspace 或 BigCode home 下。
- plan 文件名由 `PlanStore` 生成，用户不通过配置指定具体 session 文件名。
- task store 目录由 Task 模块管理。
- `BIGCODE_TASK_LIST_ID` 可以覆盖默认 task list id，但必须经过安全清洗。

## RuntimeConfig

配置加载完成后建议归一化为一个不可变对象：

```py
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ResolvedModel:
    ref: str
    provider: str
    model_key: str
    model_id: str
    base_url: str
    api_key_env: str | None
    default_headers: dict[str, str]
    capabilities: "ModelCapabilities"
    context_window: int | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class RuntimeConfig:
    config_roots: list[Path]
    default_model_ref: str
    models: dict[str, ResolvedModel]
    workspace_roots: list[Path]
    permission_context: "ToolPermissionContext"
    hooks: dict
    mcp: "McpConfig"
    skill_roots: list[Path]
    agent_roots: list[Path]
    instruction_roots: list[Path]
    plan_default_dir: Path
    task_default_list_id: str | None = None
    config_errors: list[str] = field(default_factory=list)
```

运行时规则：

- Agent Loop 只接收已解析的 `ResolvedModel`，不要在调用模型时再次猜测 provider。
- Context 使用 `ResolvedModel.capabilities` 处理图片、thinking、parallel tool calls 等 API 差异。
- Tool 和 MCP 使用同一份权限上下文。
- Hooks、MCP、Skill 的加载错误进入 `config_errors` 或各自 discovery error，不应污染普通用户消息。

## 安全和审计

- 配置中的 token、headers、env、api key 只能通过环境变量或本地进程环境读取，不能写入 transcript。
- 错误日志可以显示字段名和 provider/server 名，不能显示密钥值。
- 所有路径配置必须解析真实路径，拒绝 symlink 穿透。
- 配置 allow 只能影响普通询问，不能覆盖 hard deny。
- MCP、Skill、外部 prompt 和外部 resource 都是外部上下文，不能覆盖 system instructions、permission rules 或 user instructions。
- 模型 provider 的 `base_url` 属于网络目标；连接前仍受网络权限和 SSRF 策略约束。

## 测试建议

单测：

- user、project、cwd-local 同名 model / MCP server / skill / agent 覆盖顺序正确。
- `/home/qt/.agents/skills` 中的 skill 能被发现，且可被项目同名 skill 覆盖。
- `model: inherit` 解析为继承父模型，非 inherit 值必须走模型注册表。
- 切换模型引用时，`base_url`、`api_key_env`、headers、capabilities 同步切换。
- 配置 JSON 局部错误不影响其他合法配置项。
- `workspace_roots` 不存在时启动失败或要求用户重配。
- 配置中的密钥值不进入 Context、transcript 或普通错误输出。

集成测试：

- 主会话默认模型来自全局配置，项目配置可以覆盖。
- subAgent 使用自己的 `model` 时连接到对应 provider 的 `base_url`。
- MCP server 和 Skill 能进入 Capability Index，但正文不自动进入 system prompt。
- Plan Mode 下配置 allow 不会允许 workspace 写入。
