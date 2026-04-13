# Godot MCP Server

[![CI](https://github.com/sterion66/godot-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/sterion66/godot-mcp-server/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A comprehensive [FastMCP](https://github.com/jlowin/fastmcp) server for Godot 4.x game development. Provides tooling for AI-assisted workflows: project management, file operations, asset discovery, GDScript development, and optional Godot execution.

<!-- mcp-name: io.github.sterion66/godot -->

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

**Security model:** All Godot projects, file writes, and Asset Library downloads are restricted to a single **workspace directory** on your machine (default `~/godot-games`). The server refuses paths outside that tree. See [Workspace setup](#workspace-setup-required).

**Code execution:** `godot_run_game` and `godot_execute_script` require `GODOT_MCP_ALLOW_GODOT_EXEC` in the server environment. **The shipped MCP JSON configs** set it to **`1`** so these tools work after copy-paste. Running `python godot_mcp_server.py` **without** that env still leaves execution **off** until you export it. To lock down an IDE install, remove the variable or set it to `0`. See [Godot execution (environment)](#godot-execution-environment).

**HTTP transport:** Binding to `0.0.0.0` or `::` exposes the MCP server on all network interfaces; prefer `127.0.0.1` unless you use a firewall or VPN.

**Symlinks:** `GODOT_MCP_ROOT` is resolved with symlinks followed; point it at a real directory you control.

## Features

### Project Management
- Auto-detect Godot projects (`project.godot`); list all projects under the workspace (`godot_list_projects`)
- Create new projects under the workspace (`godot_create_project`)
- Parse project settings and configuration; list autoloads and editor plugin state (read-only for plugins)
- Refresh project cache after changes

### File Operations
- Read/write scenes (`.tscn`), scripts (`.gd`), resources (`.tres`)
- Create new scripts, scenes, resources from templates
- Edit existing files with content replacement
- Validate scene and script syntax

### Code Generation
- CharacterBody2D/3D movement controllers
- State machine patterns
- Custom resources
- Node scripts with signals/exports

### Asset Management
- Discover assets by extension, pattern, glob
- Find unused assets
- Search file contents with regex
- **Search & download from Godot Asset Library**
- **Browse Godot repos on GitHub**

### Runtime Integration
- Find Godot executable
- Check Godot version
- Run game headless
- Execute GDScript code
- Read Godot logs
- File watcher configuration

## Installation

```bash
# From PyPI-style editable install (recommended for contributors)
pip install -e .

# Or minimal deps only
pip install -r requirements.txt
```

Console entry point (after `pip install -e .`): `godot-mcp-server` (same as `python godot_mcp_server.py`).

## Workspace setup (required)

The MCP server **only** operates on Godot projects that live under one root folder. This keeps assistants from reading or writing arbitrary paths on your system.

1. **Create the default folder** (once per machine):

   ```bash
   mkdir -p ~/godot-games
   ```

2. **Put every Godot game there** — each game is its own subdirectory containing `project.godot`, for example:

   ```text
   ~/godot-games/
     my-platformer/     ← open this folder in your editor
       project.godot
       ...
     another-game/
       project.godot
   ```

3. **Open your IDE workspace** inside `~/godot-games/.../your-game` (or a parent folder under `~/godot-games`) so the MCP process can discover `project.godot` from the current working directory.

4. **Custom location:** Set an absolute path before starting the server:

   ```bash
   export GODOT_MCP_ROOT="/path/to/your/godot-games"
   python godot_mcp_server.py
   ```

   In Cursor / Claude / other MCP configs, add `env`:

   ```json
   "env": {
     "GODOT_MCP_ROOT": "/path/to/your/godot-games"
   }
   ```

   If unset, the default is **`$HOME/godot-games`**. The server creates that directory on startup if it does not exist.

5. **Inspect at runtime:** call the tool `godot_get_workspace` or read the resource `project://workspace` to see the active workspace path.

If tools report that no project was found, your cwd is probably outside the workspace, or `project_path` points outside `GODOT_MCP_ROOT`.

## Godot execution (environment)

Running the game or executing GDScript **runs code** as your user (same as starting Godot from a terminal). The server only enables `godot_run_game` / `godot_execute_script` when **`GODOT_MCP_ALLOW_GODOT_EXEC`** is set to an accepted “on” value.

**Shipped configs (default yes):** every example JSON in this repo (`mcp_config.json`, `mcp_config.cursor.json`, etc.) includes:

```json
"env": {
  "GODOT_MCP_ALLOW_GODOT_EXEC": "1"
}
```

So if you copy one of those into your IDE, execution is **allowed** without extra steps. Merge other keys (e.g. `GODOT_MCP_ROOT`) into the same `env` object.

**CLI without MCP config:** running `python godot_mcp_server.py` does **not** set this variable; execution tools stay **blocked** until you `export GODOT_MCP_ALLOW_GODOT_EXEC=1` (or use a wrapper script).

**Stricter setups:** delete `GODOT_MCP_ALLOW_GODOT_EXEC` from `env`, or set it to `0` / `false` / `no` / `off`, then restart the MCP client.

Accepted “on” values: `1`, `true`, `yes`, `on` (case-insensitive). Call `godot_get_workspace` and check `godot_exec_allowed` to confirm.

## Usage

### CLI (stdio - for Claude Code/Cursor)
```bash
python godot_mcp_server.py
```

### HTTP Server
```bash
python godot_mcp_server.py --transport http --port 8765
```

## Configuration

Set `GODOT_MCP_ROOT` in the MCP server `env` if you do not use the default `~/godot-games`. See [Workspace setup](#workspace-setup-required). Shipped snippets below include `GODOT_MCP_ALLOW_GODOT_EXEC`; see [Godot execution (environment)](#godot-execution-environment).

**Path to the server:** Examples use `"args": ["godot_mcp_server.py"]` assuming the MCP process runs with its **working directory** at the folder that contains the script (e.g. you cloned this repo). If the server fails to start, replace that with the **absolute path** to `godot_mcp_server.py` on your machine.

### Install in your editor

Expand a section and paste the JSON into the file your tool expects. The same `godot` server block is in the repo as `mcp_config.*.json` for copy-paste.

<details>
<summary><strong>Cursor</strong></summary>

**macOS / Linux (project or user config)** — Settings → MCP → *Add new global MCP server* **or** create `.cursor/mcp.json` in a project root:

```json
{
  "mcpServers": {
    "godot": {
      "command": "python3",
      "args": ["godot_mcp_server.py"],
      "env": {
        "GODOT_MCP_ALLOW_GODOT_EXEC": "1"
      }
    }
  }
}
```

**Windows** — if `python` is not on PATH for the MCP host, use the launcher or full path to `python.exe`, and prefer an **absolute** path in `args`:

```json
{
  "mcpServers": {
    "godot": {
      "command": "cmd",
      "args": ["/c", "python", "C:\\path\\to\\godot-mcp-server\\godot_mcp_server.py"],
      "env": {
        "GODOT_MCP_ALLOW_GODOT_EXEC": "1"
      }
    }
  }
}
```

Merge `GODOT_MCP_ROOT` into `env` if you do not use `~/godot-games` (see [Workspace setup](#workspace-setup-required)).

</details>

<details>
<summary><strong>Visual Studio Code</strong></summary>

Use the MCP / agent settings your VS Code build provides (often **Settings → MCP** or a project `.vscode/mcp.json`, depending on version and extensions). Paste the same structure as Cursor:

```json
{
  "mcpServers": {
    "godot": {
      "command": "python3",
      "args": ["godot_mcp_server.py"],
      "env": {
        "GODOT_MCP_ALLOW_GODOT_EXEC": "1"
      }
    }
  }
}
```

Use an absolute path in `args` if the workspace folder is not the repo root.

</details>

<details>
<summary><strong>Claude Desktop</strong></summary>

Edit the app config file:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "godot": {
      "command": "python3",
      "args": ["/absolute/path/to/godot_mcp_server.py"],
      "env": {
        "GODOT_MCP_ALLOW_GODOT_EXEC": "1"
      }
    }
  }
}
```

Restart Claude Desktop after saving.

</details>

<details>
<summary><strong>Claude Code</strong> (CLI)</summary>

Merge into `~/.claude/settings.json` (or use `claude mcp add` if your CLI supports it — check `claude mcp --help`):

```json
{
  "mcpServers": {
    "godot": {
      "command": "python3",
      "args": ["godot_mcp_server.py"],
      "env": {
        "GODOT_MCP_ALLOW_GODOT_EXEC": "1"
      },
      "description": "Godot 4.x game development server"
    }
  }
}
```

</details>

<details>
<summary><strong>Windsurf</strong> (Codeium)</summary>

**macOS / Linux:** edit `~/.codeium/windsurf/mcp_config.json`, or use **CMD+SHIFT+P → “Windsurf: Configure MCP Servers”**.

```json
{
  "mcpServers": {
    "godot": {
      "command": "python3",
      "args": ["godot_mcp_server.py"],
      "env": {
        "GODOT_MCP_ALLOW_GODOT_EXEC": "1"
      },
      "description": "Godot 4.x game development - project, scenes, scripts, assets, runtime"
    }
  }
}
```

</details>

<details>
<summary><strong>Roo Code</strong></summary>

Paste into Roo’s MCP settings (project or global), same JSON shape as above. Repo copy: `roo_code_mcp.json`.

```json
{
  "mcpServers": {
    "godot": {
      "command": "python3",
      "args": ["godot_mcp_server.py"],
      "env": {
        "GODOT_MCP_ALLOW_GODOT_EXEC": "1"
      },
      "description": "Godot 4.x game dev - project, scenes, scripts, assets, runtime"
    }
  }
}
```

</details>

<details>
<summary><strong>Generic JSON</strong> (any MCP host)</summary>

Minimal stdio config — same as `mcp_config.json` in this repo:

```json
{
  "mcpServers": {
    "godot": {
      "command": "python3",
      "args": ["godot_mcp_server.py"],
      "env": {
        "GODOT_MCP_ALLOW_GODOT_EXEC": "1"
      },
      "description": "Godot 4.x game development server - file ops, asset management, runtime integration"
    }
  }
}
```

</details>

### Limits (DoS / abuse)

- **Regex search (`godot_search_content`):** Pattern length capped; match list capped; files larger than 2 MiB are skipped. Malicious regex can still be expensive—keep patterns simple.
- **Asset zip download:** Maximum download size, per-file uncompressed size, total uncompressed size, and file count are enforced before extraction (see constants near the top of `godot_mcp_server.py`).
- **Asset Library IDs:** `asset_id` for `godot_get_asset_info` / `godot_download_asset` must be numeric digits only.

### HTTP Mode (Remote)

Use loopback unless you know what you are doing:

```bash
python godot_mcp_server.py --transport http --host 127.0.0.1 --port 8765
```

Binding to all interfaces (`--host 0.0.0.0`) logs a warning and exposes MCP to your LAN with **no authentication** in this server.

Then use `serverUrl` instead of `command`:

```json
{
  "mcpServers": {
    "godot": {
      "serverUrl": "http://localhost:8765/mcp"
    }
  }
}
```

<details>
<summary><strong>Why not duplicate HTTP + stdio?</strong></summary>

Only run **one** transport to the same project (stdio **or** HTTP), not both at once, to avoid conflicting MCP sessions.

</details>

## Tools Reference

40 MCP tools are registered in `godot_mcp_server.py` (search for `@mcp.tool`). Summary:

| Tool | Description |
|------|-------------|
| `godot_get_workspace` | Show MCP sandbox directory (`GODOT_MCP_ROOT`) |
| `godot_find_project` | Locate project root (walk-up or workspace scan) |
| `godot_list_projects` | List every `project.godot` under the workspace |
| `godot_create_project` | Create `project.godot` + starter scene under the workspace |
| `godot_get_project_info` | Get project details |
| `godot_get_project_settings` | Parse project.godot (includes `editor_plugins_enabled` read-only) |
| `godot_get_project_files` | List all project files |
| `godot_refresh_project` | Rescan scenes/scripts/resources counts |
| `godot_list_scenes` | List `.tscn` files |
| `godot_list_scripts` | List `.gd` files |
| `godot_list_resources` | List `.tres` files |
| `godot_list_autoload` | List autoload singletons |
| `godot_list_editor_plugins` | Installed addons vs enabled in project.godot (enable plugins in the Godot editor) |
| `godot_find_assets` | Find by extension |
| `godot_find_unused_files` | Find unreferenced assets |
| `godot_find_by_pattern` | Glob pattern search |
| `godot_search_content` | Regex search in files |
| `godot_create_script` | Create new GDScript |
| `godot_create_scene` | Create new scene |
| `godot_create_resource` | Create new resource |
| `godot_create_code_template` | Template scripts |
| `godot_read_scene` | Parse scene file |
| `godot_read_script` | Parse GDScript |
| `godot_validate_scene` | Validate scene |
| `godot_validate_script` | Validate syntax |
| `godot_edit_file` | Replace content |
| `godot_write_file` | Write file |
| `godot_get_file_info` | File metadata |
| `godot_find_godot_executable` | Locate Godot |
| `godot_check_version` | Godot version |
| `godot_run_game` | Run headless |
| `godot_execute_script` | Run GDScript |
| `godot_get_log` | Read logs |
| `godot_watch_files` | Configure watcher |
| `godot_get_node_info` | Node type hints |
| `godot_generate_uid` | Generate UID |
| `godot_search_assetlib` | Search Asset Library |
| `godot_get_asset_info` | Asset details |
| `godot_download_asset` | Download / extract asset (promotes nested `addons/`) |
| `godot_browse_github` | Search GitHub |

**Editor plugins:** this server does not write `[editor_plugins]` in `project.godot` (avoids conflicting with an open editor). Install addons via tools above; the user enables plugins in **Project Settings → Plugins** in Godot; use `godot_list_editor_plugins` to verify.

## Resources

| Resource | URI | Description |
|----------|-----|-------------|
| Project Info | `project://info` | Basic project info |
| Project Overview | `project://overview` | File counts |
| Workspace | `project://workspace` | MCP sandbox path (`GODOT_MCP_ROOT`) |
| Runtime | `project://runtime` | Godot version + workspace path |

## Examples

### Create a Platformer Player
```python
# Using template
create_code_template("character_body_2d", "Player")
```

### Find Assets
```python
# All PNG files
find_assets([".png", ".jpg"])

# Unused assets
find_unused_files()
```

### Search Asset Library
```python
search_assetlib("platformer")
# => [{title: "PlatformerController2D", ...}]

get_asset_info("1062")
# => {title, author, description, license, download_url}
```

### Run Game Headless

Requires `GODOT_MCP_ALLOW_GODOT_EXEC` (included in the shipped MCP JSON configs).

```python
run_game(headless=True, quit_after_seconds=30)
```

## Requirements

- Python 3.10+
- Dependencies: `fastmcp`, `urllib3` (see `pyproject.toml`)

## Development

```bash
pip install -e ".[dev]"
ruff check godot_mcp_server.py tests
pytest
```

Continuous integration runs on **Python 3.10–3.14** (see `.github/workflows/ci.yml`). [Dependabot](.github/dependabot.yml) opens weekly PRs for pip and GitHub Actions.

Optional: `pip install pre-commit && pre-commit install` uses `.pre-commit-config.yaml`.

## Publish to GitHub

1. Create an empty repository on GitHub (no README/license if you already have them locally), e.g. `godot-mcp-server`.
2. Add the remote and push:

```bash
cd /path/to/godot-mcp-server
git remote add origin https://github.com/YOUR_USER/godot-mcp-server.git
git push -u origin main
```

Or with [GitHub CLI](https://cli.github.com/): `gh repo create godot-mcp-server --public --source=. --remote=origin --push`

After the first push, CI runs on every push and PR. Replace `sterion66` in the README badge URLs if you use a different account or organization.

## License

[MIT](LICENSE)