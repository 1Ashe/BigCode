# BigCode 代码阅读路线

这份文档是给 Python 初学者看的阅读顺序。源码里已经补了中文 docstring 和关键行内注释，建议先按下面路线读，不要一开始就逐文件乱翻。

## 1. 先看入口

- `bigcode/cli.py`：命令行入口，理解 `bigcode run`、`bigcode repl`、`bigcode doctor` 怎么分流。
- `bigcode/config/loader.py`：理解配置怎么从 `.bigcode/settings.json`、`models.json`、环境变量合并成 `RuntimeConfig`。
- `bigcode/agent/session.py`：主流程。重点读 `AgentSession.run_turn()`，这是“用户输入 -> 模型回复 -> 工具执行 -> 再喂回模型”的核心循环。

## 2. 再看消息和上下文

- `bigcode/context/messages.py`：BigCode 内部怎么表示用户消息、助手消息、工具调用和工具结果。
- `bigcode/context/builder.py`：每次请求模型前怎么重新构建上下文。
- `bigcode/context/normalizer.py`：内部消息怎么转换成 Claude Messages API 格式。
- `bigcode/context/compact.py`：对话太长时怎么压缩。

## 3. 然后看工具系统
- `bigcode/tools/base.py`：所有工具的共同接口。
- `bigcode/tools/registry.py`：工具如何注册、如何把 schema 给模型。
- `bigcode/tools/runner.py`：工具执行主流程，重点读 `run_one()`。
- `bigcode/tools/permissions.py`：权限判断，重点读 `decide_permission()` 和 `classify_bash()`。

## 4. 最后看扩展能力
- `bigcode/hooks/`：在会话、上下文、工具执行前后插入逻辑。
- `bigcode/skills/`：扫描和加载 `SKILL.md`。
- `bigcode/subagents/`：子代理和后台任务。
- `bigcode/mcp/`：可选的 MCP 外部资源、prompt、工具接入。

## 常见 Python 概念
- `dataclass`：主要用来定义“只装数据”的对象，例如 `RuntimeConfig`、`ToolRunResult`。
- `BaseModel`：来自 Pydantic，用来校验模型传给工具的参数。
- `async def` / `await`：异步函数。这里用于模型请求、工具执行、hook 调用。
- `Path`：来自 `pathlib`，比字符串路径更安全方便。
- 前导下划线函数，例如 `_parse_models()`：表示模块内部辅助函数，不是主要对外接口。
