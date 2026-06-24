# BigCode

A compact Python coding agent CLI inspired by Claude Code. Supports interactive REPL, Plan Mode, subagents, MCP tools, and multiple LLM backends (Anthropic / OpenAI-compatible).

## Quick Start

### 1. Environment

```bash
conda create -n bigcode python=3.12
conda activate bigcode
```

### 2. Install dependencies

```bash
pip install -e .
```

### 3. Configure your API key

BigCode reads from environment variables — no config file needed for auth.

**Anthropic (default):**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

**OpenAI-compatible:**
```bash
export OPENAI_API_KEY="sk-..."
```

### 4. Run

```bash
bigcode repl
```

Type `/help` in the REPL for available slash commands.

## Configuration

Model and permission settings live under `.bigcode/` in your project or home directory:

- `.bigcode/models.json` — define model profiles and select a default
- `.bigcode/settings.json` — permissions, hooks, sandbox, and other runtime options

See `bigcode config` for diagnostic details about your current setup.

## Features

- **Streaming REPL** — model output and tool calls streamed to the terminal
- **Plan Mode** — explore and design before writing code (`/plan` or `EnterPlanMode`)
- **Subagents** — spawn background or synchronous sub-agents with filtered tool access
- **MCP** — connect external tool servers via FastMCP
- **Skills** — reusable prompt+tool bundles (built-in: code-review, test-debug, repo-map)
- **Context compaction** — auto-compact long conversations to stay within context windows
- **Bash sandboxing** — optional file-system and network isolation for shell commands

## Dependencies

- Python >= 3.12
- anthropic, openai — LLM backends
- pydantic, httpx, rich, prompt_toolkit — runtime
