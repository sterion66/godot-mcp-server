# Security

## Scope

This MCP server runs on your machine with your user privileges. It **sandboxes file and project access** to a single workspace directory (`GODOT_MCP_ROOT`, default `~/godot-games`). It does **not** sandbox the Python process itself.

Optional tools **`godot_run_game`** and **`godot_execute_script`** run the Godot binary and user GDScript; they are gated by **`GODOT_MCP_ALLOW_GODOT_EXEC`**.

HTTP mode (`--transport http`) can expose the MCP endpoint on your network; bind to **`127.0.0.1`** unless you understand the risk.

## Reporting vulnerabilities

Please **do not** open a public issue for undisclosed security problems.

1. Open a **private security advisory** on GitHub (Repository → Security → Advisories), or  
2. Email the repository maintainers if contact is listed on the profile.

Include: affected version/commit, steps to reproduce, and impact. We aim to acknowledge within a few business days.

## Hardening tips

- Keep `GODOT_MCP_ROOT` pointed at a dedicated games folder, not your home directory root.
- For IDE use, prefer stdio MCP over wide-area HTTP.
- Review Asset Library downloads and zip extraction limits in `godot_mcp_server.py` if you fork.
