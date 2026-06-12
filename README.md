# BigCode

BigCode is a Python coding-agent implementation based on the design documents in `doc/`.

Start the interactive REPL:

```bash
conda run -n BigCode python -m bigcode --cwd /home/qt/BigCode
```

Configuration is read from `~/.bigcode`, project `.bigcode`, and cwd-local `.bigcode`.
At minimum, configure an OpenAI-compatible model in `.bigcode/models.json` or `~/.bigcode/models.json`.

FastMCP support is optional. If `fastmcp` is not installed, MCP tools report a clear dependency error while local tools, skills, plans, tasks, and the REPL continue to work.

