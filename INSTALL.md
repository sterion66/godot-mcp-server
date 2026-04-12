# Godot MCP Server - Installation Guide

Quick setup for all popular AI coding editors.

## Prerequisites

```bash
pip install fastmcp
# or
pip install -r requirements.txt
```

## Quick Install (All Editors)

1. Copy `godot_mcp_server.py` to your project or home folder
2. Use the config file for your editor below

---

## Claude Code

**File:** `~/.claude/settings.json`

```json
{
  "mcpServers": {
    "godot": {
      "command": "python",
      "args": ["godot_mcp_server.py"]
    }
  }
}
```

---

## Cursor IDE

**File:** `.cursor/mcp.json` or cursor settings

```json
{
  "mcpServers": {
    "godot": {
      "command": "python",
      "args": ["godot_mcp_server.py"]
    }
  }
}
```

---

## Windsurf (Codeium)

**File:** `~/.codeium/windsurf/mcp_config.json`

```json
{
  "mcpServers": {
    "godot": {
      "command": "python",
      "args": ["godot_mcp_server.py"]
    }
  }
}
```

*Or via CMD+SHIFT+P → "Windsurf: Configure MCP Servers"*

---

## Roo Code

**File:** `roo_code_mcp.json` (project) or settings

```json
{
  "mcpServers": {
    "godot": {
      "command": "python",
      "args": ["godot_mcp_server.py"]
    }
  }
}
```

---

## Generic (stdio)

Run directly:
```bash
python godot_mcp_server.py
```

HTTP mode:
```bash
python godot_mcp_server.py --transport http --port 8765
```

---

## Config Files Included

| File | Editor |
|------|--------|
| `mcp_config.claude.json` | Claude Code |
| `mcp_config.cursor.json` | Cursor |
| `windsurf.json` | Windsurf |
| `roo_code_mcp.json` | Roo Code |
| `mcp_config.json` | Generic |

---

## Troubleshooting

**Server not found:**
- Ensure `python` is in your PATH
- Use absolute path if needed: `/usr/bin/python`

**Port in use (HTTP):**
```bash
python godot_mcp_server.py --port 8766
```

**No Godot project found:**
- Server auto-detects `project.godot` in current directory
- Use tool `godot_find_project` to locate manually

---

## Quick Test

```bash
python godot_mcp_server.py --help
```

Should output:
```
usage: godot_mcp_server.py [-h] [--transport {stdio,http}] [--port PORT] [--host HOST]
```

---

## Features (~40 MCP tools)

- Project management & settings (including `godot_list_projects`, `godot_create_project`)
- Scene/Script/Resource operations  
- Asset discovery & search
- GDScript templates
- Godot Asset Library search
- GitHub repo search
- Game execution (headless)
- And more...

See `README.md` for full documentation.