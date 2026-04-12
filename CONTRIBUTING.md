# Contributing

Thanks for helping improve the Godot MCP server.

## Setup

```bash
git clone https://github.com/sterion66/godot-mcp-server.git
cd godot-mcp-server
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Checks

```bash
ruff check godot_mcp_server.py tests
pytest
python -m py_compile godot_mcp_server.py
```

CI runs the same on Python 3.10–3.14.

## Changes

- Keep edits focused; match existing style in `godot_mcp_server.py`.
- New MCP tools should follow existing `@mcp.tool` patterns and workspace rules (`GODOT_MCP_ROOT`).
- Update **README.md** tool lists if you add or rename public tools.

## Pull requests

- Describe **what** changed and **why**.
- Note if behavior is security- or compatibility-sensitive (Godot 4.x, Python versions).
