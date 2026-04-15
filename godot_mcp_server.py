#!/usr/bin/env python3
"""
Godot MCP Server - A comprehensive FastMCP server for Godot game development.

Provides tools for:
- Project management and analysis
- File creation, reading, and editing
- Asset discovery and management (install addons; editor plugin enable is user-driven in Godot)
- GDScript development and debugging
- Scene and resource file handling

Usage:
    python godot_mcp_server.py [--transport stdio|http] [--port PORT]

    Optional env: GODOT_MCP_ROOT — absolute path containing all Godot projects (default ~/godot-games).
    Optional env: GODOT_EXECUTABLE — absolute path to the Godot binary (overrides automatic discovery).
    If unset, the server resolves Godot via GODOT_EXECUTABLE, then OS-specific search (macOS: mdfind +
    find under Applications; Windows: where.exe; Unix: PATH and common dirs).
    Optional env: GODOT_ENGINE_FEATURE — first config/features entry for new projects (default 4.4 for
    broad Godot 4.4+ compatibility). Set e.g. to 4.6 to match a newer editor on your machine.
"""

import logging
import os
import re
import sys
import uuid
import hashlib
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime

try:
    from fastmcp import FastMCP
except ImportError:
    print(
        "Error: fastmcp not installed. Install with: pip install fastmcp",
        file=sys.stderr,
    )
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("GodotMCPServer")

mcp = FastMCP(
    name="Godot MCP Server",
    instructions="""Comprehensive Godot game development server: project management, file operations,
asset discovery, GDScript development, and scene analysis (Godot 4.x formats).

All projects, file writes, and Asset Library downloads are confined to a single workspace directory
(default ~/godot-games, or GODOT_MCP_ROOT). Open your editor with cwd inside that folder, or pass
project_path under that root. Tools refuse paths outside the workspace.

Use godot_create_project to add project.godot plus a starter scene under the workspace, then (when
GODOT_MCP_ALLOW_GODOT_EXEC=1) run Godot with --import so .godot/ is created like opening the project
in the editor. Godot has no separate CLI wizard; this matches the engine's project format and import step.

Editor plugins: this server can install addons (e.g. godot_download_asset) under res://addons/, but it does
not write [editor_plugins] in project.godot (that would fight the open Godot editor and trigger reload prompts).
The user enables plugins in Project Settings → Plugins. Agents should use godot_list_editor_plugins and
godot_get_project_settings (editor_plugins_enabled) to verify before continuing.

godot_run_game and godot_execute_script are disabled until the user sets GODOT_MCP_ALLOW_GODOT_EXEC=1
(arbitrary code execution). Regex search and zip downloads have size limits.
    """,
)

PROJECT_FILE = "project.godot"
GODOT_DIR = ".godot"
SCENE_EXTENSIONS = {".tscn", ".scn", ".escn"}
SCRIPT_EXTENSIONS = {".gd", ".gdshader"}
RESOURCE_EXTENSIONS = {".tres", ".res"}
ASSET_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".wav",
    ".ogg",
    ".mp3",
    ".glb",
    ".gltf",
    ".blend",
    ".svg",
    ".tscn",
    ".scn",
}
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".gde",
    ".godot",
}

# Env GODOT_MCP_ROOT: absolute path; all Godot projects and MCP file ops must stay under this tree.
_DEFAULT_WORKSPACE_DIRNAME = "godot-games"

# Human-in-the-loop: run_game / execute_script default OFF until GODOT_MCP_ALLOW_GODOT_EXEC is set.
_ENV_ALLOW_GODOT_EXEC = "GODOT_MCP_ALLOW_GODOT_EXEC"

# Optional: absolute path to Godot binary (MCP often runs with a minimal PATH; use for Homebrew / .app).
_ENV_GODOT_EXECUTABLE = "GODOT_EXECUTABLE"

# Optional: first entry of config/features in new project.godot; default targets Godot 4.4+ compatibility.
_ENV_ENGINE_FEATURE = "GODOT_ENGINE_FEATURE"

# Regex search: limit pattern size and match volume to reduce ReDoS / DoS risk.
_MAX_SEARCH_PATTERN_LEN = 512
_MAX_SEARCH_MATCHES = 2000
_MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024  # skip larger files

# Asset Library zip: download and extraction limits.
_MAX_ZIP_DOWNLOAD_BYTES = 100 * 1024 * 1024
_MAX_ZIP_UNCOMPRESSED_ENTRY_BYTES = 50 * 1024 * 1024
_MAX_ZIP_EXTRACT_TOTAL_BYTES = 400 * 1024 * 1024
_MAX_ZIP_EXTRACT_FILES = 10000

# Default patch level for config/features in godot_create_project (Godot 4.4+ baseline for portability).
_DEFAULT_ENGINE_FEATURE = "4.4"


def _default_project_config_features() -> list:
    """PackedStringArray feature entries for new projects: engine patch + Forward Plus."""
    v = os.environ.get(_ENV_ENGINE_FEATURE, "").strip()
    patch = v if v else _DEFAULT_ENGINE_FEATURE
    return [patch, "Forward Plus"]


def get_mcp_workspace_root() -> Path:
    """Directory that sandboxes every project path, download, and write operation."""
    env = os.environ.get("GODOT_MCP_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / _DEFAULT_WORKSPACE_DIRNAME).resolve()


def is_path_within_workspace(path: Path, workspace: Optional[Path] = None) -> bool:
    """True if path is the workspace root or a path inside it (after resolve)."""
    ws = (workspace or get_mcp_workspace_root()).resolve()
    target = path.resolve()
    if target == ws:
        return True
    try:
        target.relative_to(ws)
        return True
    except ValueError:
        return False


def ensure_mcp_workspace_exists() -> None:
    """Create the workspace directory if missing (idempotent)."""
    root = get_mcp_workspace_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Could not create MCP workspace %s: %s", root, e)


def error_if_project_outside_workspace(project_path: Optional[str]) -> Optional[str]:
    """If an explicit project_path was given, it must lie under the MCP workspace."""
    if not project_path:
        return None
    p = Path(project_path).expanduser().resolve()
    if not is_path_within_workspace(p, get_mcp_workspace_root()):
        return (
            "project_path is outside the MCP workspace "
            "(set GODOT_MCP_ROOT or use the default ~/godot-games)"
        )
    return None


def godot_exec_allowed() -> bool:
    """True if the user opted in to running the Godot binary (import, run game, execute script)."""
    v = os.environ.get(_ENV_ALLOW_GODOT_EXEC, "").strip().lower()
    return v in ("1", "true", "yes", "on")


def gate_godot_exec_blocked(tool_label: str) -> Optional[dict]:
    """
    If Godot process execution is not allowed, return an error dict for the tool response.
    Otherwise return None.
    """
    if godot_exec_allowed():
        return None
    return {
        "error": (
            f"{tool_label} is disabled by default (human-in-the-loop). "
            "Running Godot (game, headless --import, or GDScript) uses your OS user privileges. "
            f"After you accept that risk, set {_ENV_ALLOW_GODOT_EXEC}=1 in your MCP server "
            "environment (e.g. Cursor mcp.json env) and restart the server."
        ),
        "blocked": True,
        "human_in_the_loop": True,
        "requires_env": f"{_ENV_ALLOW_GODOT_EXEC}=1",
    }


def validate_asset_library_id(asset_id: str) -> Optional[str]:
    """Return an error message if asset_id is not a non-empty digit string."""
    s = (asset_id or "").strip()
    if not s or not s.isdigit():
        return "asset_id must be a numeric Asset Library id (digits only)"
    return None


@dataclass
class GodotProject:
    path: Path
    name: str = ""
    engine_version: str = ""
    main_scene: str = ""
    config_version: int = 5
    features: list = field(default_factory=list)
    display: dict = field(default_factory=dict)
    autoload: dict = field(default_factory=dict)
    editor_plugins_enabled: list = field(default_factory=list)
    input: dict = field(default_factory=dict)
    physics: dict = field(default_factory=dict)
    rendering: dict = field(default_factory=dict)
    custom: dict = field(default_factory=dict)


@dataclass
class GodotNode:
    name: str
    type: str
    parent: Optional[str] = None
    unique_id: Optional[str] = None
    properties: dict = field(default_factory=dict)
    resources: list = field(default_factory=list)
    children: list = field(default_factory=list)


@dataclass
class GodotScript:
    path: Path
    class_name: Optional[str] = None
    extends: Optional[str] = None
    imports: list = field(default_factory=list)
    exports: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    annotations: list = field(default_factory=list)
    functions: list = field(default_factory=list)
    inner_classes: list = field(default_factory=list)


def find_project_root(start_path: Optional[Path] = None) -> Optional[Path]:
    """Find project.godot by walking up from start_path; only inside the MCP workspace."""
    workspace = get_mcp_workspace_root()
    if start_path is None:
        start_path = Path.cwd()
    else:
        start_path = Path(start_path)

    current = start_path.resolve()
    if not is_path_within_workspace(current, workspace):
        return None

    for _ in range(10):
        if not is_path_within_workspace(current, workspace):
            break
        if (current / PROJECT_FILE).exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent

    resolved_start = start_path.resolve()
    if (resolved_start / PROJECT_FILE).exists() and is_path_within_workspace(
        resolved_start, workspace
    ):
        return resolved_start
    return None


_DISCOVER_PROJECTS_MAX = 200


def discover_project_roots_in_workspace(
    max_projects: int = _DISCOVER_PROJECTS_MAX,
) -> list[Path]:
    """Find directories containing project.godot under the MCP workspace (for agent discovery)."""
    workspace = get_mcp_workspace_root().resolve()
    roots: list[Path] = []
    try:
        for project_file in workspace.rglob(PROJECT_FILE):
            if len(roots) >= max_projects:
                break
            if ".godot" in project_file.parts:
                continue
            par = project_file.parent.resolve()
            if par not in roots:
                roots.append(par)
    except OSError as e:
        logger.warning("discover_project_roots_in_workspace: %s", e)
    roots.sort(key=lambda p: str(p).casefold())
    return roots


def safe_path(base: Path, user_path: str) -> Optional[Path]:
    """Resolve user path relative to base, rejecting paths outside base."""
    if not user_path:
        return None

    base_resolved = Path(base).resolve()
    path = Path(user_path)

    if path.is_absolute():
        resolved = path.resolve()
    else:
        resolved = (base_resolved / path).resolve()

    try:
        resolved.relative_to(base_resolved)
        return resolved
    except ValueError:
        return None


def resolve_new_project_folder(relative_path: str) -> tuple[Optional[Path], Optional[str]]:
    """
    Resolve a subdirectory under the MCP workspace for a new project.godot.
    Rejects absolute paths, empty/invalid segments, .., and creating at workspace root.
    """
    raw = (relative_path or "").strip()
    if not raw:
        return None, "relative_path is required (e.g. my_game or demos/puzzle)"
    if Path(raw).is_absolute():
        return None, "relative_path must be relative to the MCP workspace (not an absolute path)"
    norm = raw.replace("\\", "/").strip("/")
    if not norm:
        return None, "relative_path must not be empty"
    parts = Path(norm).parts
    if ".." in parts:
        return None, "relative_path must not contain .."
    workspace = get_mcp_workspace_root().resolve()
    candidate = (workspace / norm).resolve()
    if not is_path_within_workspace(candidate, workspace):
        return None, "relative_path escapes the MCP workspace"
    if candidate == workspace:
        return None, "relative_path must name a subdirectory (not the workspace root itself)"
    return candidate, None


def validate_main_scene_filename(main_scene: str) -> Optional[str]:
    """Main scene file must live in project root as a simple .tscn file name."""
    s = (main_scene or "").strip()
    if not s.endswith(".tscn"):
        return "main_scene must be a .tscn file name (e.g. main.tscn)"
    if "/" in s or "\\" in s:
        return "main_scene must be a single file name in the project root (no subfolders)"
    if s in (".", "..") or ".." in s:
        return "invalid main_scene file name"
    return None


def resolve_project_directory(project_path: Optional[str] = None) -> Optional[Path]:
    """Return a directory containing project.godot under the MCP workspace, or None."""
    workspace = get_mcp_workspace_root()
    if project_path:
        root = Path(project_path).expanduser().resolve()
        if not is_path_within_workspace(root, workspace):
            return None
        if (root / PROJECT_FILE).exists():
            return root
        return None
    return find_project_root()


def resolve_project_path(
    user_path: str, project_path: Optional[str] = None
) -> tuple[Optional[Path], Optional[str]]:
    """Resolve user_path to an absolute path under the Godot project root."""
    if not (user_path or "").strip():
        return None, "Empty path"

    workspace = get_mcp_workspace_root()
    if project_path:
        candidate = Path(project_path).expanduser().resolve()
        if not is_path_within_workspace(candidate, workspace):
            return None, (
                "project_path is outside the MCP workspace "
                "(set GODOT_MCP_ROOT or use a path under the workspace, default ~/godot-games)"
            )

    root = resolve_project_directory(project_path)
    if not root:
        return None, "No Godot project found"
    resolved = safe_path(root, user_path)
    if not resolved:
        return None, "Path escapes project root"
    return resolved, None


def safe_glob_pattern(pattern: str) -> bool:
    """Reject glob patterns that could escape the project root."""
    if not pattern or pattern.startswith(("/", "\\")):
        return False
    if ".." in pattern:
        return False
    return True


def _parse_packed_string_array_value(value: str) -> list:
    """Parse PackedStringArray(\"a\", \"b\") from a project.godot value (editor_plugins, etc.)."""
    s = (value or "").strip()
    if not s or s == "PackedStringArray()":
        return []
    return re.findall(r'"([^"]*)"', s)


def _read_plugin_cfg_display_name(plugin_cfg: Path) -> str:
    try:
        txt = plugin_cfg.read_text(encoding="utf-8")
    except OSError:
        return ""
    in_plugin = False
    for line in txt.split("\n"):
        ls = line.strip()
        if ls.startswith("[") and ls.endswith("]"):
            in_plugin = ls == "[plugin]"
            continue
        if in_plugin and ls.startswith("name="):
            return ls.split("=", 1)[1].strip().strip('"')
    return plugin_cfg.parent.name


def _discover_addon_plugins(project_root: Path) -> list:
    addons = project_root / "addons"
    if not addons.is_dir():
        return []
    out = []
    for child in sorted(addons.iterdir()):
        if not child.is_dir():
            continue
        pc = child / "plugin.cfg"
        if not pc.is_file():
            continue
        res_path = f"res://addons/{child.name}/plugin.cfg"
        out.append(
            {
                "plugin_cfg": res_path,
                "folder": child.name,
                "name": _read_plugin_cfg_display_name(pc),
            }
        )
    return out


def parse_project_godot(project_path: Path) -> GodotProject:
    """Parse a Godot project.godot file."""
    project = GodotProject(path=project_path.parent)

    if not project_path.exists():
        return project

    content = project_path.read_text(encoding="utf-8")
    current_section = ""

    for line in content.split("\n"):
        raw_line = line
        line = line.strip()

        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1]
            continue

        if "=" not in line or line.startswith(";"):
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value_stripped = value.strip()
        value = value_stripped.strip('"')

        if current_section == "editor_plugins" and key == "enabled":
            project.editor_plugins_enabled = _parse_packed_string_array_value(
                raw_line.split("=", 1)[1].strip()
            )
            continue

        if current_section == "application":
            if key == "config/name":
                project.name = value
            elif key == "run/main_scene":
                project.main_scene = value
        elif current_section == "":
            if key == "config_version":
                project.config_version = int(value)
        elif current_section == "rendering":
            project.rendering[key] = value
        elif current_section == "display":
            project.display[key] = value
        elif current_section == "physics":
            project.physics[key] = value
        elif current_section == "input":
            project.input[key] = value

    engine_pattern = r'engine\.version\s*=\s*"([^"]+)"'
    match = re.search(engine_pattern, content)
    if match:
        project.engine_version = match.group(1)

    version_major = re.search(r"major\s*=\s*(\d+)", content)
    version_minor = re.search(r"minor\s*=\s*(\d+)", content)
    if version_major:
        project.engine_version = f"{version_major.group(1)}.{version_minor.group(1) if version_minor else '0'}"

    features_match = re.search(
        r"config/features\s*=\s*PackedStringArray\(([^)]+)\)", content
    )
    if features_match:
        features = [f.strip().strip('"') for f in features_match.group(1).split(",")]
        project.features = features

    autoload_section = re.search(r"\[autoload\](.*?)(?=\n\[|\Z)", content, re.DOTALL)
    if autoload_section:
        for line in autoload_section.group(1).split("\n"):
            line = line.strip()
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"')
                project.autoload[key] = value

    return project


def generate_uid() -> str:
    """Generate a Godot-compatible unique ID."""
    random_bytes = uuid.uuid4().bytes
    hash_obj = hashlib.sha256(random_bytes)
    return hash_obj.hexdigest()[:22]


def find_files_by_extension(root: Path, extensions: set, relative: bool = True) -> list:
    """Find all files with given extensions in the project."""
    files = []
    godot_dir = root / GODOT_DIR

    try:
        for ext in extensions:
            for path in root.rglob(f"*{ext}"):
                if godot_dir in path.parts:
                    continue
                if relative:
                    files.append(str(path.relative_to(root)))
                else:
                    files.append(str(path))
    except Exception as e:
        logger.warning(f"Error finding files: {e}")

    return sorted(files)


def parse_tscn_scene(content: str) -> dict:
    """Parse a TSCN scene file and extract structure."""
    result = {
        "format": None,
        "uid": None,
        "external_resources": [],
        "internal_resources": [],
        "nodes": [],
        "connections": [],
    }

    lines = content.split("\n")
    section = ""
    current_node = None

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("[gd_scene"):
            result["format"] = re.search(r"format=(\d+)", stripped)
            result["uid"] = re.search(r'uid="([^"]+)"', stripped)
            if result["format"]:
                result["format"] = result["format"].group(1)
            if result["uid"]:
                result["uid"] = result["uid"].group(1)
            continue

        if stripped == "[ext_resource]":
            section = "ext_resource"
            continue
        elif stripped == "[sub_resource]":
            section = "sub_resource"
            continue
        elif stripped == "[node]":
            section = "node"
            current_node = {"name": "", "type": "", "properties": {}}
            continue
        elif stripped == "[connection]":
            section = "connection"
            continue
        elif stripped.startswith("[node ") and stripped.endswith("]"):
            section = "node"
            if current_node and current_node.get("name"):
                result["nodes"].append(current_node)
            current_node = {"name": "", "type": "", "properties": {}}
            match = re.search(r'name="([^"]+)"', stripped)
            if match:
                current_node["name"] = match.group(1)
            match = re.search(r'type="([^"]+)"', stripped)
            if match:
                current_node["type"] = match.group(1)
            match = re.search(r'parent="([^"]+)"', stripped)
            if match:
                current_node["parent"] = match.group(1)
            continue

        if stripped == "[connection]":
            if current_node and current_node.get("name"):
                result["nodes"].append(current_node)
            section = "connection"
            current_node = None
            continue

        if not stripped or stripped.startswith(";"):
            continue

        if section == "ext_resource":
            if "path=" in stripped:
                match = re.search(r'path="([^"]+)"', stripped)
                if match:
                    result["external_resources"].append(match.group(1))

        elif section == "sub_resource":
            if "type=" in stripped:
                match = re.search(r'type="([^"]+)"', stripped)
                if match:
                    res_type = match.group(1)
                    res_id = re.search(r'id="([^"]+)"', stripped)
                    result["internal_resources"].append(
                        {"type": res_type, "id": res_id.group(1) if res_id else None}
                    )

        elif section == "node":
            if current_node is None:
                continue

            if "=" in stripped:
                key, _, value = stripped.partition("=")
                key = key.strip()
                value = value.strip()

                if key == "name":
                    current_node["name"] = value.strip('"')
                elif key == "type":
                    current_node["type"] = value.strip('"')
                elif key == "parent":
                    current_node["parent"] = value.strip('"')
                elif key == "unique_id":
                    current_node["unique_id"] = value.strip('"')
                else:
                    current_node["properties"][key] = value

        elif section == "connection":
            if "signal=" in stripped:
                match = re.search(r'signal="([^"]+)"', stripped)
                if match:
                    result["connections"].append({"signal": match.group(1)})
            elif "method=" in stripped:
                match = re.search(r'method="([^"]+)"', stripped)
                if match:
                    result["connections"][-1]["method"] = match.group(1)

    if current_node and current_node.get("name"):
        result["nodes"].append(current_node)

    return result


def parse_gd_script(content: str, file_path: Path = None) -> GodotScript:
    """Parse a GDScript file and extract its structure."""
    script = GodotScript(path=file_path or Path(""))

    class_name_match = re.search(r"^class_name\s+(\w+)", content, re.MULTILINE)
    if class_name_match:
        script.class_name = class_name_match.group(1)

    extends_match = re.search(r"^extends\s+([^\s#]+)", content, re.MULTILINE)
    if extends_match:
        script.extends = extends_match.group(1)

    import_pattern = r"^@onready\s+var\s+(\w+)\s*:\s*(\w+)"
    for match in re.finditer(import_pattern, content, re.MULTILINE):
        script.exports.append(
            {"name": match.group(1), "type": match.group(2), "onready": True}
        )

    export_pattern = r"@export(?:_([a-z_]+))?\s+var\s+(\w+)\s*:?\s*(\w+)?"
    for match in re.finditer(export_pattern, content, re.MULTILINE):
        script.exports.append(
            {
                "name": match.group(2),
                "type": match.group(3) if match.group(3) else "Variant",
                "annotation": match.group(1) if match.group(1) else "export",
            }
        )

    signal_pattern = r"^signal\s+(\w+)(?:\(([^)]+)\))?"
    for match in re.finditer(signal_pattern, content, re.MULTILINE):
        script.signals.append({"name": match.group(1), "params": match.group(2)})

    func_pattern = r"^func\s+(\w+)\s*\(([^)]*)\)"
    for match in re.finditer(func_pattern, content, re.MULTILINE):
        params = match.group(2).strip()
        script.functions.append(
            {
                "name": match.group(1),
                "params": [
                    p.strip().split(":")[0].strip()
                    for p in params.split(",")
                    if p.strip()
                ],
            }
        )

    inner_class_pattern = r"^class\s+(\w+)\s+(?:extends\s+([^\s]+)\s+)?:"
    for match in re.finditer(inner_class_pattern, content, re.MULTILINE):
        script.inner_classes.append({"name": match.group(1), "extends": match.group(2)})

    export_range_pattern = r"@export_range\(([^)]+)\)\s+var\s+(\w+)"
    for match in re.finditer(export_range_pattern, content, re.MULTILINE):
        args = match.group(1).split(",")
        script.exports.append(
            {
                "name": match.group(2),
                "annotation": "export_range",
                "min": args[0] if len(args) > 0 else "0",
                "max": args[1] if len(args) > 1 else "100",
            }
        )

    return script


def get_node_type_hints(node_type: str) -> dict:
    """Get property hints for common Godot node types."""
    node_hints = {
        "Node": {
            "properties": ["name", "scene_file_path"],
            "signals": ["ready", "process_frame"],
        },
        "Node2D": {
            "properties": ["position", "rotation", "scale", "z_index", "z_as_relative"],
            "signals": [],
        },
        "Node3D": {
            "properties": [
                "position",
                "rotation",
                "scale",
                "global_position",
                "global_rotation",
            ],
            "signals": [],
        },
        "Control": {
            "properties": [
                "size",
                "anchor",
                "offset",
                "pivot_offset",
                "grow_horizontal",
                "grow_vertical",
            ],
            "signals": ["gui_input", "resized"],
        },
        "CanvasItem": {
            "properties": ["modulate", "self_modulate", "visible"],
            "signals": ["draw", "visibility_changed"],
        },
    }
    return node_hints.get(node_type, {"properties": [], "signals": []})


def generate_gdscript(
    class_name: str,
    extends: str = "Node",
    include_ready: bool = True,
    include_export: bool = False,
    exports: list = None,
    signals: list = None,
    functions: list = None,
) -> str:
    """Generate a complete GDScript class."""
    lines = []

    if extends:
        lines.append(f"extends {extends}")

    lines.append("")
    lines.append(f"class_name {class_name}")
    lines.append("")
    lines.append("")
    lines.append("# Called when the node enters the scene tree for the first time.")
    if include_ready:
        lines.append("func _ready() -> void:")
        lines.append("\tpass")
    else:
        lines.append("func _ready() -> void:")
        lines.append("\tpass")

    lines.append("")
    lines.append(
        "# Called every frame. 'delta' is the elapsed time since the previous frame."
    )
    lines.append("func _process(delta: float) -> void:")
    lines.append("\tpass")

    if exports:
        lines.append("")
        lines.append("# Exported properties")
        for exp in exports:
            exp_type = exp.get("type", "Variant")
            exp_name = exp.get("name", "property")
            lines.append(f"@export var {exp_name}: {exp_type}")

    if signals:
        lines.append("")
        for sig in signals:
            sig_name = sig.get("name", "signal_name")
            sig_params = sig.get("params", "")
            lines.append(f"signal {sig_name}({sig_params})")

    if functions:
        lines.append("")
        for func in functions:
            func_name = func.get("name", "function_name")
            func_params = func.get("params", "")
            lines.append(f"func {func_name}({func_params}) -> void:")
            lines.append("\tpass")

    return "\n".join(lines)


def generate_scene(
    root_type: str, root_name: str, child_nodes: list = None, resources: list = None
) -> str:
    """Generate a TSCN scene file."""
    lines = []
    uid = generate_uid()

    lines.append(f'[gd_scene load_steps=1 format=3 uid="uid://{uid}"]')
    lines.append("")
    lines.append(f'[node name="{root_name}" type="{root_type}"]')

    if child_nodes:
        for child in child_nodes:
            parent = child.get("parent", "..")
            name = child.get("name", "Node")
            ntype = child.get("type", "Node")
            lines.append(f'[node name="{name}" type="{ntype}" parent="{parent}"]')

    if resources:
        for i, res in enumerate(resources):
            res_type = res.get("type", "Resource")
            lines.append(f'[sub_resource type="{res_type}" id="{res_type}_{i}"]')

    lines.append("")
    return "\n".join(lines)


def generate_resource(resource_type: str, properties: list = None) -> str:
    """Generate a .tres resource file."""
    lines = []
    uid = generate_uid()

    lines.append(f'[gd_resource type="{resource_type}" format=3 uid="uid://{uid}"]')
    lines.append("")

    if properties:
        for prop in properties:
            name = prop.get("name", "property")
            value = prop.get("value", "")
            lines.append(f"{name} = {value}")
    else:
        lines.append("[resource]")
        lines.append("")

    lines.append("")
    return "\n".join(lines)


@mcp.tool(
    name="godot_find_project",
    annotations={
        "title": "Find Godot Project",
        "description": "Locate the Godot project root directory",
        "readOnlyHint": True,
    },
)
def find_project(path: Optional[str] = None) -> str:
    """
    Find the Godot project root by searching for project.godot.

    Args:
        path: Optional starting path to search upward from (defaults to MCP workspace root).
            Walk-up is tried first; if that fails, the workspace is scanned for project.godot.

    Returns:
        Path to the Godot project root or error message
    """
    workspace = get_mcp_workspace_root()
    search_path = Path(path) if path else get_mcp_workspace_root()
    start = search_path.resolve()
    if not is_path_within_workspace(start, workspace):
        return (
            f"No Godot project found: search path is outside the MCP workspace ({workspace}). "
            "Open a folder under that directory or set GODOT_MCP_ROOT."
        )
    project_root = find_project_root(search_path)

    if project_root:
        return (
            f"Found Godot project at: {project_root} "
            f"(MCP workspace: {workspace})"
        )
    roots = discover_project_roots_in_workspace()
    if not roots:
        return (
            f"No Godot project found under {workspace}. "
            "Create or clone a Godot project there (must contain project.godot), "
            "or use godot_list_projects to confirm the workspace is readable."
        )
    if len(roots) == 1:
        return (
            f"Found Godot project at: {roots[0]} "
            f"(MCP workspace: {workspace}; scanned from workspace — use this path as project_path for other tools)"
        )
    preview = ", ".join(str(r) for r in roots[:15])
    more = f" … (+{len(roots) - 15} more)" if len(roots) > 15 else ""
    return (
        f"Multiple Godot projects under {workspace} ({len(roots)}). "
        f"Pass project_path to the one you want. Roots: {preview}{more}. "
        "Use godot_list_projects for names and paths."
    )


@mcp.tool(
    name="godot_list_projects",
    annotations={
        "title": "List Godot projects in workspace",
        "description": "Enumerate project.godot roots under the MCP workspace (GODOT_MCP_ROOT / ~/godot-games)",
        "readOnlyHint": True,
    },
)
def list_projects() -> dict:
    """
    List every Godot project under the MCP sandbox directory.

    Use this when the MCP process cwd is not inside a project or you need to pick among several games.

    Returns:
        workspace root and a list of {root, name} for each project.godot found
    """
    ws = get_mcp_workspace_root()
    roots = discover_project_roots_in_workspace()
    projects: list[dict] = []
    for r in roots:
        try:
            gp = parse_project_godot(r / PROJECT_FILE)
            projects.append(
                {
                    "root": str(r),
                    "name": gp.name or r.name,
                }
            )
        except OSError:
            projects.append({"root": str(r), "name": r.name})
    return {
        "workspace": str(ws),
        "count": len(projects),
        "projects": projects,
    }


@mcp.tool(
    name="godot_get_workspace",
    annotations={
        "title": "Get MCP workspace root",
        "description": "Directory that contains all Godot projects and MCP file operations (GODOT_MCP_ROOT)",
        "readOnlyHint": True,
    },
)
def get_workspace() -> dict:
    """
    Return the sandbox directory for this server.

    Every project path, write, and Asset Library download must stay under this tree unless
    you change GODOT_MCP_ROOT and restart the server.

    Returns:
        Absolute workspace path and configuration hints
    """
    ws = get_mcp_workspace_root()
    return {
        "workspace": str(ws),
        "env_var": "GODOT_MCP_ROOT",
        "default_if_unset": str(Path.home() / _DEFAULT_WORKSPACE_DIRNAME),
        "note": (
            "Create Godot projects under this folder (each needs project.godot). "
            "godot_create_project can run Godot --import to initialize .godot/ when GODOT_MCP_ALLOW_GODOT_EXEC=1."
        ),
        "godot_exec_allowed": godot_exec_allowed(),
        "godot_exec_opt_in_env": _ENV_ALLOW_GODOT_EXEC,
        "godot_exec_note": (
            f"godot_run_game / godot_execute_script require {_ENV_ALLOW_GODOT_EXEC}=1 "
            "(default off; human-in-the-loop)."
        ),
        "default_engine_feature": _default_project_config_features()[0],
        "engine_feature_env": _ENV_ENGINE_FEATURE,
        "engine_feature_note": (
            f"godot_create_project uses config/features {_DEFAULT_ENGINE_FEATURE}+ unless "
            f"{_ENV_ENGINE_FEATURE} is set."
        ),
        "editor_plugins_note": (
            "Editor plugins are enabled only in the Godot editor (Project Settings → Plugins). "
            "Use godot_list_editor_plugins / godot_get_project_settings to read state; do not expect MCP to toggle [editor_plugins]."
        ),
    }


def _run_godot_import_new_project(
    project_root: Path,
    godot_path: Optional[str] = None,
    timeout_seconds: int = 240,
) -> dict:
    """
    Run Godot 4.3+ with --path ... --import --headless --quit so .godot/ exists and resources import,
    like the first open in the editor. (Godot does not ship a one-shot "new project" CLI.)
    """
    import subprocess

    gate = gate_godot_exec_blocked("godot_create_project (--import)")
    if gate:
        return {
            "ran": False,
            "status": "skipped_exec_not_enabled",
            "note": gate.get("error"),
            "requires_env": gate.get("requires_env"),
        }

    if not godot_path:
        resolved = find_godot_executable()
        if not resolved.get("found"):
            return {
                "ran": False,
                "status": "skipped_godot_not_found",
                "note": resolved.get("error", "Godot executable not found"),
            }
        godot_path = resolved.get("path")

    exe = Path(godot_path or "")
    if not exe.is_file():
        return {"ran": False, "status": "skipped_godot_not_found", "note": f"Not a file: {exe}"}

    root = project_root.resolve()
    args = [str(exe), "--headless", "--path", str(root), "--import", "--quit"]

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(root),
        )
        out: dict = {
            "ran": True,
            "status": "success" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
            "command": args,
            "stdout_tail": (proc.stdout or "")[-8000:],
            "stderr_tail": (proc.stderr or "")[-8000:],
        }
        if proc.returncode != 0:
            out["hint"] = (
                "If Godot is older than 4.3, --import may be missing; upgrade or open the project once in the editor."
            )
        return out
    except subprocess.TimeoutExpired:
        return {
            "ran": True,
            "status": "timeout",
            "error": f"Godot import timed out after {timeout_seconds}s",
        }
    except FileNotFoundError:
        return {
            "ran": False,
            "status": "skipped_godot_not_found",
            "note": f"Executable not found: {exe}",
        }


@mcp.tool(
    name="godot_create_project",
    annotations={
        "title": "Create Godot Project",
        "description": (
            "Create project.godot + starter scene, then optionally run Godot --import to build .godot/ "
            "(same as opening the project in the editor; requires GODOT_MCP_ALLOW_GODOT_EXEC=1)"
        ),
    },
)
def create_project(
    relative_path: str,
    display_name: Optional[str] = None,
    main_scene: str = "main.tscn",
    root_node_type: str = "Node2D",
    root_node_name: str = "Main",
    config_features: Optional[list] = None,
    run_godot_import: bool = True,
    godot_path: Optional[str] = None,
) -> dict:
    """
    Create a Godot project under the MCP workspace: writes project.godot and a starter .tscn (what the
    Project Manager also creates on disk). When run_godot_import is true and GODOT_MCP_ALLOW_GODOT_EXEC=1,
    runs the Godot editor in headless import mode (--import) so the .godot/ cache folder exists and
    resources are processed—this is the engine's own initialization step; there is no separate
    "create project" binary in Godot.

    Args:
        relative_path: Folder under the workspace (e.g. my_game or demos/puzzle). Not absolute.
        display_name: Project name in the project manager (default: last path segment).
        main_scene: Main scene file name in the project root (default: main.tscn).
        root_node_type: Root node type for the starter scene (default: Node2D).
        root_node_name: Root node name in the starter scene (default: Main).
        config_features: Optional list for config/features (default: 4.4 + Forward Plus, or GODOT_ENGINE_FEATURE).
        run_godot_import: If True, attempt Godot --import after writing files (requires exec opt-in).
        godot_path: Optional path to the Godot executable for the import step.

    Returns:
        Dict with success, paths, and godot_import status (or error).
    """
    ensure_mcp_workspace_exists()
    target, err = resolve_new_project_folder(relative_path)
    if err:
        return {"success": False, "error": err}

    ms_err = validate_main_scene_filename(main_scene)
    if ms_err:
        return {"success": False, "error": ms_err}

    if (target / PROJECT_FILE).exists():
        return {"success": False, "error": f"project already exists at {target / PROJECT_FILE}"}

    dn = (display_name or "").strip() or target.name
    if any(c in dn for c in ('"', "\n", "\r")):
        return {"success": False, "error": "display_name must not contain quotes or newlines"}

    scene_path = safe_path(target, main_scene)
    if not scene_path:
        return {"success": False, "error": "invalid main_scene path"}

    features = config_features if config_features else _default_project_config_features()
    inner = ", ".join(f'"{str(f).strip()}"' for f in features if str(f).strip())
    if not inner:
        return {"success": False, "error": "config_features must contain at least one feature string"}

    try:
        target.mkdir(parents=True, exist_ok=True)
        project_ini = (
            "; Godot project generated by Godot MCP Server\n\n"
            "config_version=5\n\n"
            "[application]\n\n"
            f'config/name="{dn}"\n'
            f'run/main_scene="res://{main_scene}"\n'
            f"config/features=PackedStringArray({inner})\n"
        )
        (target / PROJECT_FILE).write_text(project_ini, encoding="utf-8")

        scene_body = generate_scene(
            root_type=root_node_type,
            root_name=root_node_name,
            child_nodes=None,
            resources=None,
        )
        scene_path.write_text(scene_body, encoding="utf-8")
    except OSError as e:
        return {"success": False, "error": str(e)}

    out: dict = {
        "success": True,
        "project_path": str(target),
        "main_scene": f"res://{main_scene}",
        "files_created": [PROJECT_FILE, main_scene],
        "note": (
            "Project files match what Godot writes; optional step runs `godot --path <dir> --import --headless --quit` "
            "to create .godot/ (Godot 4.3+; --import requires a recent 4.x build)."
        ),
    }

    if run_godot_import:
        out["godot_import"] = _run_godot_import_new_project(target, godot_path=godot_path)
    else:
        out["godot_import"] = {
            "ran": False,
            "status": "skipped_by_request",
            "note": "Set run_godot_import=true and GODOT_MCP_ALLOW_GODOT_EXEC=1 to run Godot --import.",
        }

    return out


@mcp.tool(
    name="godot_get_project_info",
    annotations={
        "title": "Get Project Info",
        "description": "Get comprehensive information about the Godot project",
        "readOnlyHint": True,
    },
)
def get_project_info(project_path: Optional[str] = None) -> dict:
    """
    Get detailed information about the Godot project.

    Args:
        project_path: Optional path to project.godot file (defaults to finding it)

    Returns:
        Dictionary with project details
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return {"error": bad}
    project_root = resolve_project_directory(project_path)
    if not project_root:
        return {"error": "No Godot project found"}
    project_file = project_root / "project.godot"

    project = parse_project_godot(project_file)

    root = project.path
    scenes = find_files_by_extension(root, SCENE_EXTENSIONS)
    scripts = find_files_by_extension(root, SCRIPT_EXTENSIONS)
    resources = find_files_by_extension(root, RESOURCE_EXTENSIONS)
    assets = []

    for ext in [".png", ".jpg", ".jpeg", ".webp", ".wav", ".ogg", ".glb"]:
        assets.extend(find_files_by_extension(root, {ext}))

    return {
        "path": str(project.path),
        "name": project.name,
        "engine_version": project.engine_version,
        "main_scene": project.main_scene,
        "config_version": project.config_version,
        "features": project.features,
        "autoload": project.autoload,
        "files": {
            "scenes": len(scenes),
            "scripts": len(scripts),
            "resources": len(resources),
            "assets": len(assets),
        },
    }


@mcp.tool(
    name="godot_list_scenes",
    annotations={
        "title": "List Scene Files",
        "description": "List all scene files in the project",
        "readOnlyHint": True,
    },
)
def list_scenes(project_path: Optional[str] = None, relative: bool = True) -> list:
    """
    List all .tscn scene files in the Godot project.

    Args:
        project_path: Optional path to project root
        relative: Return paths relative to project root (default True)

    Returns:
        List of scene file paths
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return [bad]
    root = resolve_project_directory(project_path)
    if not root:
        return ["No Godot project found"]

    return find_files_by_extension(root, SCENE_EXTENSIONS, relative)


@mcp.tool(
    name="godot_list_scripts",
    annotations={
        "title": "List Script Files",
        "description": "List all GDScript files in the project",
        "readOnlyHint": True,
    },
)
def list_scripts(project_path: Optional[str] = None, relative: bool = True) -> list:
    """
    List all .gd script files in the Godot project.

    Args:
        project_path: Optional path to project root
        relative: Return paths relative to project root (default True)

    Returns:
        List of script file paths
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return [bad]
    root = resolve_project_directory(project_path)
    if not root:
        return ["No Godot project found"]

    return find_files_by_extension(root, SCRIPT_EXTENSIONS, relative)


@mcp.tool(
    name="godot_list_resources",
    annotations={
        "title": "List Resource Files",
        "description": "List all resource files in the project",
        "readOnlyHint": True,
    },
)
def list_resources(project_path: Optional[str] = None, relative: bool = True) -> list:
    """
    List all .tres resource files in the Godot project.

    Args:
        project_path: Optional path to project root
        relative: Return paths relative to project root (default True)

    Returns:
        List of resource file paths
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return [bad]
    root = resolve_project_directory(project_path)
    if not root:
        return ["No Godot project found"]

    return find_files_by_extension(root, RESOURCE_EXTENSIONS, relative)


@mcp.tool(
    name="godot_find_assets",
    annotations={
        "title": "Find Assets",
        "description": "Find assets by extension pattern",
        "readOnlyHint": True,
    },
)
def find_assets(
    extensions: list, project_path: Optional[str] = None, relative: bool = True
) -> list:
    """
    Find assets by extension.

    Args:
        extensions: List of extensions to search (e.g., [".png", ".glb"])
        project_path: Optional path to project root
        relative: Return paths relative to project root

    Returns:
        List of asset paths
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return [bad]
    root = resolve_project_directory(project_path)
    if not root:
        return ["No Godot project found"]

    ext_set = set(extensions)
    return find_files_by_extension(root, ext_set, relative)


@mcp.tool(
    name="godot_read_scene",
    annotations={
        "title": "Read Scene File",
        "description": "Parse and read a .tscn scene file",
        "readOnlyHint": True,
    },
)
def read_scene(scene_path: str, project_path: Optional[str] = None) -> dict:
    """
    Read and parse a scene file.

    Args:
        scene_path: Path to the .tscn file
        project_path: Optional project root (defaults to discovery from cwd)

    Returns:
        Dictionary with scene structure
    """
    path, err = resolve_project_path(scene_path, project_path)
    if err:
        return {"error": err}

    if not path.exists():
        return {"error": f"Scene file not found: {path}"}

    try:
        content = path.read_text(encoding="utf-8")
        return parse_tscn_scene(content)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(
    name="godot_read_script",
    annotations={
        "title": "Read GDScript",
        "description": "Parse and read a GDScript file",
        "readOnlyHint": True,
    },
)
def read_script(script_path: str, project_path: Optional[str] = None) -> dict:
    """
    Read and parse a GDScript file.

    Args:
        script_path: Path to the .gd file
        project_path: Optional project root (defaults to discovery from cwd)

    Returns:
        Dictionary with script structure
    """
    path, err = resolve_project_path(script_path, project_path)
    if err:
        return {"error": err}

    if not path.exists():
        return {"error": f"Script file not found: {path}"}

    try:
        content = path.read_text(encoding="utf-8")
        script = parse_gd_script(content, path)

        return {
            "path": str(path),
            "class_name": script.class_name,
            "extends": script.extends,
            "exports": script.exports,
            "signals": script.signals,
            "functions": script.functions,
            "inner_classes": script.inner_classes,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(
    name="godot_create_script",
    annotations={
        "title": "Create GDScript",
        "description": "Create a new GDScript file",
    },
)
def create_script(
    class_name: str,
    extends: str = "Node",
    project_path: Optional[str] = None,
    include_ready: bool = True,
    exports: Optional[list] = None,
    signals: Optional[list] = None,
    functions: Optional[list] = None,
) -> str:
    """
    Create a new GDScript file.

    Args:
        class_name: Name for the new class
        extends: Class to extend (default: Node)
        project_path: Optional project root path
        include_ready: Include _ready() function (default: True)
        exports: List of exports (format: [{"name": "var_name", "type": "int"}])
        signals: List of signals (format: [{"name": "signal_name", "params": "value"}])
        functions: List of custom functions

    Returns:
        Path to created script
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return bad
    root = resolve_project_directory(project_path)
    if not root:
        return "No Godot project found"

    script_path = safe_path(root, f"{class_name.lower()}.gd")
    if not script_path:
        return "Error: Invalid or unsafe script path"
    content = generate_gdscript(
        class_name=class_name,
        extends=extends,
        include_ready=include_ready,
        exports=exports,
        signals=signals,
        functions=functions,
    )

    script_path.write_text(content, encoding="utf-8")
    return f"Created: {script_path}"


@mcp.tool(
    name="godot_create_scene",
    annotations={
        "title": "Create Scene",
        "description": "Create a new .tscn scene file",
    },
)
def create_scene(
    root_type: str,
    root_name: str,
    child_nodes: Optional[list] = None,
    resources: Optional[list] = None,
    project_path: Optional[str] = None,
) -> str:
    """
    Create a new scene file.

    Args:
        root_type: Root node type (e.g., Node, Node2D, Node3D)
        root_name: Name for the root node
        child_nodes: List of child nodes
        resources: List of sub-resources
        project_path: Optional project root

    Returns:
        Path to created scene
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return bad
    root = resolve_project_directory(project_path)
    if not root:
        return "No Godot project found"

    scene_path = safe_path(root, f"{root_name.lower()}.tscn")
    if not scene_path:
        return "Error: Invalid or unsafe scene path"
    content = generate_scene(
        root_type=root_type,
        root_name=root_name,
        child_nodes=child_nodes,
        resources=resources,
    )

    scene_path.write_text(content, encoding="utf-8")
    return f"Created: {scene_path}"


@mcp.tool(
    name="godot_create_resource",
    annotations={
        "title": "Create Resource",
        "description": "Create a new .tres resource file",
    },
)
def create_resource(
    resource_type: str,
    properties: Optional[list] = None,
    resource_name: Optional[str] = None,
    project_path: Optional[str] = None,
) -> str:
    """
    Create a new resource file.

    Args:
        resource_type: Type of resource (e.g., Resource, Theme, StyleBox)
        properties: List of properties
        resource_name: Optional name for the file
        project_path: Optional project root

    Returns:
        Path to created resource
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return bad
    root = resolve_project_directory(project_path)
    if not root:
        return "No Godot project found"

    name = resource_name if resource_name else f"new_{resource_type.lower()}"
    res_path = safe_path(root, f"{name}.tres")
    if not res_path:
        return "Error: Invalid or unsafe resource path"
    content = generate_resource(resource_type=resource_type, properties=properties)

    res_path.write_text(content, encoding="utf-8")
    return f"Created: {res_path}"


@mcp.tool(
    name="godot_edit_file",
    annotations={
        "title": "Edit Godot File",
        "description": "Edit an existing Godot file",
    },
)
def edit_file(
    file_path: str,
    old_content: str,
    new_content: str,
    project_path: Optional[str] = None,
) -> str:
    """
    Edit a Godot file by replacing content.

    Args:
        file_path: Path to the file
        old_content: Content to find
        new_content: Replacement content
        project_path: Optional project root

    Returns:
        Success message or error
    """
    path, err = resolve_project_path(file_path, project_path)
    if err:
        return f"Error: {err}"

    if not path.exists():
        return f"File not found: {path}"

    try:
        content = path.read_text(encoding="utf-8")

        if old_content not in content:
            return "Old content not found in file"

        new_file_content = content.replace(old_content, new_content)
        path.write_text(new_file_content, encoding="utf-8")

        return f"Edited: {path}"
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool(
    name="godot_write_file",
    annotations={"title": "Write Godot File", "description": "Write content to a file"},
)
def write_file(file_path: str, content: str, project_path: Optional[str] = None) -> str:
    """
    Write content to a file.

    Args:
        file_path: Path to the file
        content: Content to write
        project_path: Optional project root

    Returns:
        Success message
    """
    path, err = resolve_project_path(file_path, project_path)
    if err:
        return f"Error: {err}"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    return f"Written: {path}"


@mcp.tool(
    name="godot_search_content",
    annotations={
        "title": "Search File Contents",
        "description": "Search for content in script and scene files",
    },
)
def search_content(
    pattern: str, file_types: Optional[list] = None, project_path: Optional[str] = None
) -> list:
    """
    Search for content in Godot files.

    Args:
        pattern: Regex pattern to search for
        file_types: File types to search (e.g., [".gd", ".tscn"])
        project_path: Optional project root

    Returns:
        List of matches with file paths and line numbers
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return [{"error": bad}]
    root = resolve_project_directory(project_path)
    if not root:
        return [{"error": "No Godot project found"}]

    if len(pattern) > _MAX_SEARCH_PATTERN_LEN:
        return [
            {
                "error": (
                    f"Pattern exceeds maximum length ({_MAX_SEARCH_PATTERN_LEN} chars); "
                    "shorten the regex to reduce ReDoS risk."
                )
            }
        ]

    types = (
        set(file_types)
        if file_types
        else SCRIPT_EXTENSIONS
        | SCENE_EXTENSIONS
        | RESOURCE_EXTENSIONS
        | TEXT_EXTENSIONS
    )

    results = []
    truncated = False

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return [{"error": f"Invalid regex: {e}"}]

    try:
        for file_path in root.rglob("*"):
            if truncated:
                break
            if file_path.is_file() and file_path.suffix in types:
                if ".godot" in file_path.parts:
                    continue

                try:
                    if file_path.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                        continue
                except OSError:
                    continue

                try:
                    content = file_path.read_text(encoding="utf-8")
                    for i, line in enumerate(content.split("\n"), 1):
                        if len(results) >= _MAX_SEARCH_MATCHES:
                            truncated = True
                            break
                        if regex.search(line):
                            results.append(
                                {
                                    "file": str(file_path.relative_to(root)),
                                    "line": i,
                                    "content": line.strip(),
                                }
                            )
                    if truncated:
                        break
                except (OSError, UnicodeError):
                    continue
    except Exception as e:
        return [{"error": str(e)}]

    if truncated:
        results.append(
            {
                "truncated": True,
                "match_limit": _MAX_SEARCH_MATCHES,
                "note": "Result list capped; narrow your regex or search fewer file types.",
            }
        )

    return results


@mcp.tool(
    name="godot_find_unused_files",
    annotations={
        "title": "Find Unused Files",
        "description": "Find assets not referenced in any scene or script",
        "readOnlyHint": True,
    },
)
def find_unused_files(project_path: Optional[str] = None) -> dict:
    """
    Find files that may not be referenced in scenes or scripts.

    Args:
        project_path: Optional project root

    Returns:
        Dictionary with potential unused files
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return {"error": bad}
    root = resolve_project_directory(project_path)
    if not root:
        return {"error": "No Godot project found"}

    all_references = set()

    for ext in SCRIPT_EXTENSIONS | SCENE_EXTENSIONS:
        for path in root.rglob(f"*{ext}"):
            if ".godot" in path.parts or not path.is_file():
                continue

            try:
                content = path.read_text(encoding="utf-8")
                for ref in re.finditer(
                    r'res://([^")\s]+\.(?:png|glb|wav|ogg|tres|tscn|gd))', content
                ):
                    all_references.add(ref.group(1))
            except (OSError, UnicodeError):
                continue

    all_assets = []
    for ext in ASSET_EXTENSIONS:
        for path in root.rglob(f"*{ext}"):
            if ".godot" in path.parts:
                continue
            rel_path = str(path.relative_to(root))
            all_assets.append(rel_path)

    unused = [a for a in all_assets if a not in all_references]

    return {
        "total_assets": len(all_assets),
        "referenced": len(all_references),
        "potentially_unused": unused[:50],
    }


@mcp.tool(
    name="godot_get_node_info",
    annotations={
        "title": "Get Node Type Info",
        "description": "Get information about a node type",
        "readOnlyHint": True,
    },
)
def get_node_info(node_type: str) -> dict:
    """
    Get information about a Godot node type.

    Args:
        node_type: Node type name (e.g., Node2D, CharacterBody2D)

    Returns:
        Dictionary with node properties and hints
    """
    hints = get_node_type_hints(node_type)

    return {
        "type": node_type,
        "properties": hints.get("properties", []),
        "signals": hints.get("signals", []),
        "hints": f"Node type '{node_type}' is available in Godot 4.x",
    }


@mcp.tool(
    name="godot_generate_uid",
    annotations={
        "title": "Generate UID",
        "description": "Generate a Godot-compatible unique ID",
        "readOnlyHint": True,
    },
)
def generate_uid_tool() -> str:
    """
    Generate a unique ID for Godot resources.

    Returns:
        A unique ID string
    """
    return generate_uid()


@mcp.tool(
    name="godot_get_project_settings",
    annotations={
        "title": "Get Project Settings",
        "description": "Get project settings from project.godot",
        "readOnlyHint": True,
    },
)
def get_project_settings(project_path: Optional[str] = None) -> dict:
    """
    Get project settings from project.godot (read-only).

    editor_plugins_enabled lists res:// paths from [editor_plugins]; the user toggles plugins in the
    Godot editor — MCP does not write that section.

    Args:
        project_path: Optional path to project.godot

    Returns:
        Dictionary with project settings
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return {"error": bad}
    project_root = resolve_project_directory(project_path)
    if not project_root:
        return {"error": "No Godot project found"}
    project_file = project_root / "project.godot"

    project = parse_project_godot(project_file)

    return {
        "name": project.name,
        "main_scene": project.main_scene,
        "features": project.features,
        "engine_version": project.engine_version,
        "autoload": project.autoload,
        "editor_plugins_enabled": project.editor_plugins_enabled,
        "display": project.display,
        "input": project.input,
    }


@mcp.tool(
    name="godot_list_autoload",
    annotations={
        "title": "List Autoload Scripts",
        "description": "List all autoload (singleton) scripts",
        "readOnlyHint": True,
    },
)
def list_autoload(project_path: Optional[str] = None) -> dict:
    """
    List autoload singletons.

    Args:
        project_path: Optional project root

    Returns:
        Dictionary with autoload scripts
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return {"error": bad}
    project_root = resolve_project_directory(project_path)
    if not project_root:
        return {"error": "No Godot project found"}
    project_file = project_root / "project.godot"

    project = parse_project_godot(project_file)

    return {"autoload": project.autoload}


@mcp.tool(
    name="godot_list_editor_plugins",
    annotations={
        "title": "List Editor Plugins",
        "description": (
            "Read-only: addons under addons/*/plugin.cfg vs [editor_plugins] in project.godot. "
            "Enabling plugins is done in the Godot editor (Project Settings → Plugins), not by MCP."
        ),
        "readOnlyHint": True,
    },
)
def list_editor_plugins(project_path: Optional[str] = None) -> dict:
    """
    List addon folders with plugin.cfg and which editor plugins are enabled in project.godot.

    Use this (and godot_get_project_settings → editor_plugins_enabled) after the user enables plugins
    in the editor to verify before continuing. MCP installs addons (e.g. godot_download_asset) but does
    not modify [editor_plugins], to avoid fighting the open editor and external-reload dialogs.

    Args:
        project_path: Optional project root

    Returns:
        installed plugins and enabled res:// paths
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return {"error": bad}
    project_root = resolve_project_directory(project_path)
    if not project_root:
        return {"error": "No Godot project found"}
    project_file = project_root / PROJECT_FILE
    project = parse_project_godot(project_file)
    installed = _discover_addon_plugins(project_root)
    enabled = list(project.editor_plugins_enabled) if project.editor_plugins_enabled else []
    return {
        "installed": installed,
        "enabled": enabled,
    }


@mcp.tool(
    name="godot_validate_scene",
    annotations={
        "title": "Validate Scene",
        "description": "Validate a scene file structure",
        "readOnlyHint": True,
    },
)
def validate_scene(scene_path: str, project_path: Optional[str] = None) -> dict:
    """
    Validate a scene file.

    Args:
        scene_path: Path to the scene file
        project_path: Optional project root (defaults to discovery from cwd)

    Returns:
        Validation results
    """
    path, err = resolve_project_path(scene_path, project_path)
    if err:
        return {"valid": False, "error": err}

    if not path.exists():
        return {"valid": False, "error": "File not found"}

    try:
        content = path.read_text(encoding="utf-8")
        parsed = parse_tscn_scene(content)

        errors = []
        warnings = []

        if not parsed.get("format"):
            errors.append("Missing format declaration")

        nodes = parsed.get("nodes", [])
        if not nodes:
            warnings.append("No nodes found in scene")

        root_nodes = [n for n in nodes if not n.get("parent")]
        if len(root_nodes) != 1:
            errors.append(f"Expected 1 root node, found {len(root_nodes)}")

        for node in nodes:
            if not node.get("type"):
                errors.append(f"Node '{node.get('name')}' missing type")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "node_count": len(nodes),
        }
    except Exception as e:
        return {"valid": False, "error": str(e)}


def _extract_godot_validation_messages(output: str) -> list[str]:
    """Extract likely compile/parser diagnostics from Godot stdout/stderr text."""
    if not output:
        return []

    messages: list[str] = []
    seen: set[str] = set()
    keywords = (
        "parser error",
        "parse error",
        "script error",
        "error:",
        "failed to compile",
        "compile error",
        "cannot infer the type",
        "identifier not declared",
        "invalid call",
        "expected",
    )

    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if any(k in lowered for k in keywords):
            msg = line
            if msg not in seen:
                messages.append(msg)
                seen.add(msg)
    return messages


def _run_godot_strict_script_validation(
    script_path: Path, project_root: Path, timeout_seconds: int = 45
) -> dict:
    """
    Run Godot in check-only mode for real parser/type diagnostics.

    This does not execute game logic; it asks Godot to compile/check the script.
    """
    import subprocess

    found = find_godot_executable()
    if not found.get("found"):
        return {
            "available": False,
            "ran": False,
            "success": None,
            "errors": [],
            "warnings": [],
            "error": "Godot executable not found for strict validation",
        }

    godot_path = found.get("path")
    args = [
        str(godot_path),
        "--headless",
        "--path",
        str(project_root),
        "--check-only",
        "--script",
        str(script_path),
    ]

    try:
        process = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(project_root),
        )
    except subprocess.TimeoutExpired:
        return {
            "available": True,
            "ran": True,
            "success": False,
            "timed_out": True,
            "errors": ["Strict validation timed out"],
            "warnings": [],
        }
    except Exception as e:
        return {
            "available": True,
            "ran": False,
            "success": False,
            "errors": [f"Strict validation failed to run: {e}"],
            "warnings": [],
        }

    combined = "\n".join(
        s for s in [process.stdout or "", process.stderr or ""] if s.strip()
    )
    diagnostics = _extract_godot_validation_messages(combined)

    errors: list[str] = []
    warnings: list[str] = []
    if process.returncode != 0:
        if diagnostics:
            errors.extend(diagnostics)
        else:
            errors.append(
                f"Godot strict check failed with exit code {process.returncode}"
            )
    else:
        warnings.extend(diagnostics)

    return {
        "available": True,
        "ran": True,
        "success": process.returncode == 0,
        "returncode": process.returncode,
        "errors": errors,
        "warnings": warnings,
        "stdout": (process.stdout or "")[:4000],
        "stderr": (process.stderr or "")[:4000],
    }


@mcp.tool(
    name="godot_validate_script",
    annotations={
        "title": "Validate Script",
        "description": "Check GDScript with basic checks and optional strict Godot parser/type validation",
        "readOnlyHint": True,
    },
)
def validate_script(
    script_path: str, project_path: Optional[str] = None, strict: bool = True
) -> dict:
    """
    Validate a GDScript file.

    Args:
        script_path: Path to the script
        project_path: Optional project root (defaults to discovery from cwd)
        strict: If true, run Godot --check-only for parser/type diagnostics when executable is available

    Returns:
        Validation results
    """
    path, err = resolve_project_path(script_path, project_path)
    if err:
        return {"valid": False, "error": err}

    if not path.exists():
        return {"valid": False, "error": "File not found"}

    try:
        content = path.read_text(encoding="utf-8")
        script = parse_gd_script(content, path)

        errors = []
        warnings = []

        if content.strip() == "":
            errors.append("Empty file")

        if not script.extends and script.class_name is None:
            warnings.append("No extends or class_name defined")

        open_braces = content.count("{")
        close_braces = content.count("}")
        if open_braces != close_braces:
            errors.append(
                f"Mismatched braces: {open_braces} open, {close_braces} close"
            )

        open_parens = content.count("(")
        close_parens = content.count(")")
        if open_parens != close_parens:
            errors.append(
                f"Mismatched parentheses: {open_parens} open, {close_parens} close"
            )

        open_brackets = content.count("[")
        close_brackets = content.count("]")
        if open_brackets != close_brackets:
            errors.append(
                f"Mismatched brackets: {open_brackets} open, {close_brackets} close"
            )

        strict_result = {
            "available": False,
            "ran": False,
            "success": None,
            "errors": [],
            "warnings": [],
        }
        if strict:
            project_root = resolve_project_directory(project_path)
            if project_root:
                strict_result = _run_godot_strict_script_validation(path, project_root)
                if strict_result.get("errors"):
                    errors.extend(
                        [f"[godot-strict] {msg}" for msg in strict_result["errors"]]
                    )
                if strict_result.get("warnings"):
                    warnings.extend(
                        [f"[godot-strict] {msg}" for msg in strict_result["warnings"]]
                    )
            else:
                warnings.append(
                    "[godot-strict] Skipped strict validation: no project root found"
                )
        else:
            warnings.append("[godot-strict] Skipped by request (strict=false)")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "class_name": script.class_name,
            "extends": script.extends,
            "functions": len(script.functions),
            "exports": len(script.exports),
            "strict_validation": strict_result,
        }
    except Exception as e:
        return {"valid": False, "error": str(e)}


@mcp.tool(
    name="godot_get_file_info",
    annotations={
        "title": "Get File Info",
        "description": "Get information about a file",
        "readOnlyHint": True,
    },
)
def get_file_info(file_path: str, project_path: Optional[str] = None) -> dict:
    """
    Get information about a file.

    Args:
        file_path: Path to the file
        project_path: Optional project root (defaults to discovery from cwd)

    Returns:
        File information
    """
    path, err = resolve_project_path(file_path, project_path)
    if err:
        return {"error": err}

    if not path.exists():
        return {"error": "File not found"}

    stat = path.stat()

    return {
        "path": str(path),
        "name": path.name,
        "extension": path.suffix,
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "is_file": path.is_file(),
        "is_directory": path.is_dir(),
    }


@mcp.tool(
    name="godot_find_by_pattern",
    annotations={
        "title": "Find Files by Pattern",
        "description": "Find files using glob pattern",
    },
)
def find_by_pattern(pattern: str, project_path: Optional[str] = None) -> list:
    """
    Find files matching a pattern.

    Args:
        pattern: Glob pattern (e.g., "**/*.gd")
        project_path: Optional project root

    Returns:
        List of matching files
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return [bad]
    root = resolve_project_directory(project_path)
    if not root:
        return ["No Godot project found"]

    if not safe_glob_pattern(pattern):
        return ["Error: Invalid glob pattern (use a relative pattern without '..')"]

    files = []

    try:
        for path in root.glob(pattern):
            if path.is_file() and ".godot" not in path.parts:
                files.append(str(path.relative_to(root)))
    except Exception as e:
        return [f"Error: {str(e)}"]

    return sorted(files)


@mcp.tool(
    name="godot_create_code_template",
    annotations={
        "title": "Create Code Template",
        "description": "Create code from template",
    },
)
def create_code_template(
    template_type: str, name: str, project_path: Optional[str] = None
) -> str:
    """
    Create code from a template.

    Template types:
    - character_body_2d: CharacterBody2D script
    - character_body_3d: CharacterBody3D script
    - node: Basic node script
    - resource: Custom resource script
    - state_machine: State machine script

    Args:
        template_type: Type of template
        name: Name for the class
        project_path: Optional project root

    Returns:
        Path to created file
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return bad
    root = resolve_project_directory(project_path)
    if not root:
        return "No Godot project found"

    templates = {
        "character_body_2d": f"""extends CharacterBody2D

class_name {name}

@export var speed: float = 200.0
@export var jump_velocity: float = -350.0
@export var gravity: float = 980.0

var direction: float = 0.0


func _physics_process(delta: float) -> void:
\tapply_gravity(delta)
\tmove_and_slide()


func apply_gravity(delta: float) -> void:
\tvelocity.y += gravity * delta
\tif is_on_floor():
\t\tvelocity.y = 0.0


func _on_input(event: InputEvent) -> void:
\tdirection = Input.get_axis("ui_left", "ui_right")
\tvelocity.x = direction * speed


func _jump() -> void:
\tif is_on_floor():
\t\tvelocity.y = jump_velocity
""",
        "character_body_3d": f"""extends CharacterBody3D

class_name {name}

@export var speed: float = 5.0
@export var jump_velocity: float = 5.0
@export var gravity: float = 15.0

var direction: Vector3 = Vector3.ZERO


func _physics_process(delta: float) -> void:
\tapply_gravity(delta)
\tmove_and_slide()


func apply_gravity(delta: float) -> void:
\tvelocity.y -= gravity * delta
\tif is_on_floor():
\t\tvelocity.y = max(0, velocity.y)


func _input(event: InputEvent) -> void:
\tdirection = Vector3(
\t\tInput.get_axis("ui_left", "ui_right"),
\t\t0,
\t\tInput.get_axis("ui_up", "ui_down")
\t)
\tdirection = direction.normalized()
\tif direction:
\t\tvelocity.x = direction.x * speed
\t\tvelocity.z = direction.z * speed
""",
        "node": f"""extends Node

class_name {name}


func _ready() -> void:
\tpass


func _process(delta: float) -> void:
\tpass
""",
        "resource": f"""extends Resource

class_name {name}

@export var value: Variant = 0
""",
        "state_machine": f"""extends Node

class_name {name}

signal state_changed(old_state: String, new_state: String)

@export var initial_state: Node

var current_state: Node = null


func _ready() -> void:
\tfor child in get_children():
\t\tif child is State:
\t\t\tchild.state_machine = self
\t\t\tchild.process_mode = ProcessMode.PROCESS_MODE_DISABLED
	
\tif initial_state:
\t\ttransition_to(initial_state.name)


func _process(delta: float) -> void:
\tif current_state:
\t\tcurrent_state.process_state(delta)


func transition_to(state_name: String) -> void:
\tvar new_state = get_node(state_name)
\t
\tif new_state == current_state or not new_state:
\t\treturn
\t
\tvar old_state = current_state
\t
\tif current_state:
\t\tcurrent_state.process_mode = ProcessMode.PROCESS_MODE_DISABLED
\t
\tcurrent_state = new_state
\tcurrent_state.process_mode = ProcessMode.PROCESS_MODE_INHERIT
\tcurrent_state.enter()
\t
\tstate_changed.emit(old_state.name if old_state else "", state_name)
""",
    }

    template = templates.get(template_type)
    if not template:
        return f"Unknown template type: {template_type}. Available: {', '.join(templates.keys())}"

    file_path = safe_path(root, f"{name.lower()}.gd")
    if not file_path:
        return "Error: Invalid or unsafe script path"
    file_path.write_text(template, encoding="utf-8")

    return f"Created: {file_path}"


@mcp.resource("project://info")
def project_info_resource() -> dict:
    """Get project information as a resource."""
    project_root = find_project_root()
    if not project_root:
        return {"error": "No Godot project found"}

    project_file = project_root / "project.godot"
    project = parse_project_godot(project_file)

    return {
        "name": project.name,
        "path": str(project.path),
        "engine_version": project.engine_version,
    }


@mcp.resource("project://overview")
def project_overview() -> dict:
    """Get project overview including file counts."""
    project_root = find_project_root()
    if not project_root:
        return {"error": "No Godot project found"}

    scenes = find_files_by_extension(project_root, SCENE_EXTENSIONS)
    scripts = find_files_by_extension(project_root, SCRIPT_EXTENSIONS)
    resources = find_files_by_extension(project_root, RESOURCE_EXTENSIONS)

    return {
        "scenes": len(scenes),
        "scripts": len(scripts),
        "resources": len(resources),
        "files": len(scenes) + len(scripts) + len(resources),
    }


@mcp.resource("project://workspace")
def project_workspace_resource() -> dict:
    """MCP sandbox: all projects and writes must live under this directory."""
    ws = get_mcp_workspace_root()
    return {
        "workspace": str(ws),
        "env_var": "GODOT_MCP_ROOT",
        "default_if_unset": str(Path.home() / _DEFAULT_WORKSPACE_DIRNAME),
        "godot_exec_allowed": godot_exec_allowed(),
        "godot_exec_opt_in_env": _ENV_ALLOW_GODOT_EXEC,
    }


@mcp.prompt("godot_scene_analysis")
def scene_analysis_prompt(scene_path: str) -> list:
    """Generate a prompt for analyzing a scene file."""
    return [
        {
            "role": "user",
            "content": f"Analyze the scene file at {scene_path} and provide information about its structure, nodes, and resources.",
        }
    ]


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Godot MCP Server")
    parser.add_argument(
        "--transport", default="stdio", choices=["stdio", "http"], help="Transport type"
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="Port for HTTP transport"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for HTTP transport")

    args = parser.parse_args()

    ensure_mcp_workspace_exists()
    logger.info(
        "Starting Godot MCP Server (transport: %s, workspace: %s)",
        args.transport,
        get_mcp_workspace_root(),
    )
    if args.transport == "http" and args.host in ("0.0.0.0", "::", "[::]"):
        logger.warning(
            "HTTP MCP is bound to %s — exposed on all interfaces; use 127.0.0.1 unless "
            "you trust every host on the network or use a firewall.",
            args.host,
        )

    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run()


def _godot_executable_from_env() -> Optional[dict]:
    """If GODOT_EXECUTABLE is set, return a find_godot_executable-style dict or None."""
    raw = os.environ.get(_ENV_GODOT_EXECUTABLE, "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    if p.is_file():
        return {
            "found": True,
            "path": str(p),
            "name": p.name,
            "source": _ENV_GODOT_EXECUTABLE,
        }
    return {
        "found": False,
        "error": (
            f"{_ENV_GODOT_EXECUTABLE} is set but is not a file: {raw}. "
            "Use the real binary (on macOS e.g. .../Godot.app/Contents/MacOS/Godot)."
        ),
    }


def _godot_search_preference(path: Path) -> tuple:
    """Prefer stable installs over ~/Downloads when multiple Godot builds exist."""
    s = str(path.resolve())
    low = s.lower()
    if "downloads" in low:
        return (2, s)
    if "applications" in low or "program files" in low:
        return (0, s)
    return (1, s)


def _godot_macos_app_binaries() -> list[Path]:
    """Godot*.app bundle executables: /Applications, ~/Applications, ~/Downloads (incl. one subfolder)."""
    import platform

    if platform.system() != "Darwin":
        return []
    roots = (
        Path("/Applications"),
        Path.home() / "Applications",
        Path.home() / "Downloads",
    )
    seen: set[str] = set()
    out: list[Path] = []
    patterns = ("Godot*.app", "*/Godot*.app")
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in patterns:
            try:
                apps = sorted(root.glob(pattern))
            except OSError:
                continue
            for app in apps:
                if not (app.is_dir() and app.name.endswith(".app")):
                    continue
                exe = app / "Contents" / "MacOS" / "Godot"
                if exe.is_file():
                    key = str(exe.resolve())
                    if key not in seen:
                        seen.add(key)
                        out.append(exe)
    return out


def _godot_macos_spotlight_executables() -> list[Path]:
    """
    macOS: find Godot.app via Spotlight when ~/Downloads is TCC-protected (glob/list fails silently).

    Direct stat of .../Contents/MacOS/Godot still works once the .app path is known from mdfind.
    """
    import platform
    import subprocess

    if platform.system() != "Darwin":
        return []
    try:
        proc = subprocess.run(
            ["/usr/bin/mdfind", "kMDItemFSName == 'Godot.app'"],
            capture_output=True,
            text=True,
            timeout=25,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if proc.returncode != 0:
        return []
    out: list[Path] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or not line.endswith(".app"):
            continue
        app = Path(line)
        exe = app / "Contents" / "MacOS" / "Godot"
        try:
            if exe.is_file():
                out.append(exe)
        except OSError:
            continue
    return out


def _godot_darwin_find_applications() -> list[Path]:
    """
    macOS: /usr/bin/find under /Applications and ~/Applications for Godot.app (complements mdfind/glob).
    """
    import platform
    import subprocess

    if platform.system() != "Darwin":
        return []
    roots = [Path("/Applications"), Path.home() / "Applications"]
    paths = [str(r) for r in roots if r.is_dir()]
    if not paths:
        return []
    try:
        proc = subprocess.run(
            ["/usr/bin/find", *paths, "-maxdepth", "6", "-name", "Godot.app", "-type", "d"],
            capture_output=True,
            text=True,
            timeout=40,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if proc.returncode != 0:
        return []
    out: list[Path] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.endswith(".app"):
            continue
        app = Path(line)
        exe = app / "Contents" / "MacOS" / "Godot"
        try:
            if exe.is_file():
                out.append(exe)
        except OSError:
            continue
    return out


def _godot_windows_where_exe() -> Optional[Path]:
    """Windows: first match from `where.exe` for common executable names (PATH)."""
    import platform
    import subprocess

    if platform.system() != "Windows":
        return None
    for name in ("godot.exe", "Godot.exe", "godot4.exe"):
        try:
            proc = subprocess.run(
                ["where.exe", name],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0:
            continue
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        if not lines:
            continue
        p = Path(lines[0])
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _gather_automatic_godot_candidates() -> list[tuple[Path, str]]:
    """
    All automatic discovery (when GODOT_EXECUTABLE is unset), de-duplicated.

    Returns:
        List of (executable Path, source tag) — source is glob | mdfind | find | where | Downloads | scan.
    """
    import platform

    out: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def add(p: Path, src: str) -> None:
        try:
            if not p.is_file():
                return
            k = str(p.resolve())
        except OSError:
            return
        if k in seen:
            return
        seen.add(k)
        out.append((p, src))

    system = platform.system()

    if system == "Darwin":
        for p in _godot_macos_app_binaries():
            add(p, "glob")
        for p in _godot_macos_spotlight_executables():
            add(p, "mdfind")
        for p in _godot_darwin_find_applications():
            add(p, "find")
    elif system == "Windows":
        w = _godot_windows_where_exe()
        if w is not None:
            add(w, "where")
        for p in _downloads_godot_win_linux():
            add(p, "Downloads")
    elif system == "Linux":
        for p in _downloads_godot_win_linux():
            add(p, "Downloads")
        for p in _godot_linux_find_downloads():
            add(p, "find")

    return out


def _godot_linux_find_downloads() -> list[Path]:
    """Linux: limited find(1) in ~/Downloads for official portable binaries (names vary by version)."""
    import platform
    import subprocess

    if platform.system() != "Linux":
        return []
    dl = Path.home() / "Downloads"
    if not dl.is_dir():
        return []
    try:
        proc = subprocess.run(
            [
                "/usr/bin/find",
                str(dl),
                "-maxdepth",
                "3",
                "(",
                "-name",
                "Godot*.linux.*",
                "-o",
                "-name",
                "Godot_*_linux*",
                "-o",
                "-name",
                "Godot*.AppImage",
                ")",
                "-type",
                "f",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if proc.returncode != 0:
        return []
    out: list[Path] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        p = Path(line)
        try:
            if p.is_file():
                out.append(p)
        except OSError:
            continue
    return out


def _downloads_godot_win_linux() -> list[Path]:
    """Portable Godot under ~/Downloads on Windows and Linux (zip extracts, etc.)."""
    import platform

    system = platform.system()
    if system not in ("Windows", "Linux"):
        return []

    d = Path.home() / "Downloads"
    if not d.is_dir():
        return []

    seen: set[str] = set()
    out: list[Path] = []

    def add(p: Path) -> None:
        if not p.is_file():
            return
        k = str(p.resolve())
        if k not in seen:
            seen.add(k)
            out.append(p)

    if system == "Windows":
        for pattern in ("Godot*.exe", "godot*.exe"):
            try:
                for p in sorted(d.glob(pattern)):
                    add(p)
            except OSError:
                continue
        try:
            for sub in d.iterdir():
                if not sub.is_dir():
                    continue
                for pattern in ("Godot*.exe", "godot*.exe"):
                    try:
                        for p in sub.glob(pattern):
                            add(p)
                    except OSError:
                        continue
        except OSError:
            pass
    else:
        for pattern in ("Godot*.AppImage", "Godot*.linux.*", "Godot_*", "godot"):
            try:
                for p in sorted(d.glob(pattern)):
                    if p.is_file():
                        add(p)
            except OSError:
                continue
        try:
            for sub in d.iterdir():
                if not sub.is_dir():
                    continue
                for p in sub.glob("Godot*"):
                    if p.is_file():
                        add(p)
                for p in sub.glob("godot*"):
                    if p.is_file():
                        add(p)
        except OSError:
            pass

    return out


@mcp.tool(
    name="godot_find_godot_executable",
    annotations={
        "title": "Find Godot Executable",
        "description": "Locate Godot executable in common locations",
        "readOnlyHint": True,
    },
)
def find_godot_executable() -> dict:
    """
    Find the Godot executable on the system.

    Resolution order when GODOT_EXECUTABLE is unset:
      macOS: glob (where readable), Spotlight mdfind, find(1) under Applications, then directory scan,
      then PATH.
      Windows: where.exe for godot.exe / Godot.exe, then Downloads glob, then directory scan, then PATH.
      Linux: Downloads / portable names, directory scan, then PATH.

    Returns:
        Dictionary with executable path and version info
    """
    import shutil
    import platform

    env_hit = _godot_executable_from_env()
    if env_hit is not None:
        return env_hit

    system = platform.system()

    auto = _gather_automatic_godot_candidates()
    if auto:
        auto.sort(key=lambda t: _godot_search_preference(t[0]))
        exe, src = auto[0]
        return {
            "found": True,
            "path": str(exe),
            "name": exe.name,
            "source": src,
        }

    possible_names = ["godot", "godot4", "godot4.0", "godot4.1"]
    if system == "Darwin":
        possible_names = ["Godot"] + possible_names
    if system == "Windows":
        possible_names = [
            n + ".exe" for n in ["godot", "godot4", "godot4.0"]
        ] + possible_names

    paths_to_check = []

    godot_dir = find_project_root()
    if godot_dir:
        paths_to_check.append(godot_dir.parent)

    paths_to_check.extend(
        [
            Path.home() / "Downloads",
            Path.home() / "Documents",
            Path("/opt/godot"),
            Path("/opt/homebrew/bin"),
            Path("/opt/local/bin"),
            Path("/usr/local/bin"),
            Path("/usr/bin"),
        ]
    )
    if system == "Darwin":
        paths_to_check.extend(
            [
                Path("/Applications/Godot.app/Contents/MacOS"),
                Path.home() / "Applications/Godot.app/Contents/MacOS",
            ]
        )

    for name in possible_names:
        for search_path in paths_to_check:
            if not search_path:
                continue
            for test_path in [
                search_path / name,
                search_path / "Godot" / name,
                search_path / "bin" / name,
            ]:
                if test_path.exists() and test_path.is_file():
                    return {"found": True, "path": str(test_path), "name": name}

    if system == "Darwin":
        which_candidates = (
            "Godot",
            "godot",
            "godot4",
            "godot4.2",
            "godot4.3",
            "godot4.4",
            "godot4.6",
        )
    elif system == "Windows":
        which_candidates = ("Godot", "godot", "godot4", "Godot.exe", "godot.exe")
    else:
        which_candidates = ("godot", "godot4")

    for w in which_candidates:
        result = shutil.which(w)
        if result:
            return {"found": True, "path": result, "name": Path(result).name, "source": "PATH"}

    return {
        "found": False,
        "error": (
            "Godot executable not found. Automatic search uses GODOT_EXECUTABLE if set; otherwise "
            "macOS (glob + mdfind + find /Applications), Windows (where.exe + Downloads), Linux "
            "(Downloads + find ~/Downloads), then common dirs and PATH. "
            f"Set {_ENV_GODOT_EXECUTABLE} to the binary path if needed."
        ),
    }


@mcp.tool(
    name="godot_run_game",
    annotations={
        "title": "Run Godot Game",
        "description": "Run the Godot game in headless mode (requires GODOT_MCP_ALLOW_GODOT_EXEC=1)",
    },
)
def run_game(
    godot_path: Optional[str] = None,
    project_path: Optional[str] = None,
    headless: bool = True,
    quit_after_seconds: Optional[int] = None,
) -> dict:
    """
    Run the Godot game.

    Args:
        godot_path: Path to Godot executable (auto-detected if not provided)
        project_path: Path to project (auto-detected if not provided)
        headless: Run without display (default True)
        quit_after_seconds: Auto-quit after N seconds

    Returns:
        Execution result with output
    """
    import subprocess
    import platform

    blocked = gate_godot_exec_blocked("godot_run_game")
    if blocked:
        return blocked

    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return {"error": bad}
    project_root = resolve_project_directory(project_path)
    if not project_root:
        return {"error": "No Godot project found"}

    if not godot_path:
        result = find_godot_executable()
        if not result.get("found"):
            return {"error": "Godot executable not found"}
        godot_path = result.get("path")

    godot_path = Path(godot_path)
    if not godot_path.exists():
        return {"error": f"Godot executable not found: {godot_path}"}

    args = [str(godot_path)]

    if headless and platform.system() != "Windows":
        args.append("--headless")
    elif headless:
        args.append("--headless")

    args.append("--path")
    args.append(str(project_root))

    if quit_after_seconds:
        args.append("--quit-after")
        args.append(str(quit_after_seconds))

    try:
        process = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=(quit_after_seconds + 10) if quit_after_seconds else 60,
            cwd=str(project_root),
        )

        return {
            "success": process.returncode == 0,
            "returncode": process.returncode,
            "stdout": process.stdout[:10000] if process.stdout else "",
            "stderr": process.stderr[:10000] if process.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"error": "Process timed out", "timed_out": True}
    except FileNotFoundError:
        return {"error": f"Executable not found: {godot_path}"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(
    name="godot_execute_script",
    annotations={
        "title": "Execute GDScript",
        "description": "Execute GDScript via Godot (requires GODOT_MCP_ALLOW_GODOT_EXEC=1; arbitrary code execution)",
    },
)
def execute_script(
    script_content: str,
    godot_path: Optional[str] = None,
    project_path: Optional[str] = None,
) -> dict:
    """
    Execute a GDScript in the Godot project context.

    Args:
        script_content: GDScript code to execute
        godot_path: Path to Godot executable
        project_path: Path to project

    Returns:
        Script execution result
    """
    import subprocess
    import tempfile

    blocked = gate_godot_exec_blocked("godot_execute_script")
    if blocked:
        return blocked

    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return {"error": bad}
    project_root = resolve_project_directory(project_path)
    if not project_root:
        return {"error": "No Godot project found"}

    if not godot_path:
        result = find_godot_executable()
        if not result.get("found"):
            return {"error": "Godot executable not found"}
        godot_path = result.get("path")

    godot_path = Path(godot_path)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".gd", delete=False, encoding="utf-8"
    ) as f:
        f.write(script_content)
        script_file = f.name

    try:
        args = [
            str(godot_path),
            "--headless",
            "--script",
            script_file,
        ]

        process = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project_root),
        )

        return {
            "success": process.returncode == 0,
            "returncode": process.returncode,
            "stdout": process.stdout[:5000] if process.stdout else "",
            "stderr": process.stderr[:5000] if process.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"error": "Script execution timed out", "timed_out": True}
    except Exception as e:
        return {"error": str(e)}
    finally:
        try:
            Path(script_file).unlink()
        except OSError:
            pass


@mcp.tool(
    name="godot_check_version",
    annotations={
        "title": "Check Godot Version",
        "description": "Check installed Godot version",
        "readOnlyHint": True,
    },
)
def check_godot_version(godot_path: Optional[str] = None) -> dict:
    """
    Check the Godot version.

    Args:
        godot_path: Path to Godot executable

    Returns:
        Version information
    """
    import subprocess

    if not godot_path:
        result = find_godot_executable()
        if not result.get("found"):
            return {"found": False, "error": "Godot executable not found"}
        godot_path = result.get("path")

    godot_path = Path(godot_path)
    if not godot_path.exists():
        return {"error": f"Godot executable not found: {godot_path}"}

    try:
        process = subprocess.run(
            [str(godot_path), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        output = process.stdout + process.stderr

        version_info = {"raw": output, "found": True}

        import re

        major = re.search(r"(\d+)\.(\d+)", output)
        if major:
            version_info["major"] = int(major.group(1))
            version_info["minor"] = int(major.group(2))
            version_info["version"] = f"{major.group(1)}.{major.group(2)}"

        if "4." in output:
            version_info["engine"] = "Godot 4.x"
        elif "3." in output:
            version_info["engine"] = "Godot 3.x"

        return version_info
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(
    name="godot_get_log",
    annotations={
        "title": "Get Godot Log",
        "description": "Read the Godot output log",
        "readOnlyHint": True,
    },
)
def get_log(project_path: Optional[str] = None, lines: int = 100) -> dict:
    """
    Get recent Godot log entries.

    Args:
        project_path: Path to project
        lines: Number of lines to read

    Returns:
        Log content
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return {"error": bad}
    project_root = resolve_project_directory(project_path)
    if not project_root:
        return {"error": "No project found"}

    log_paths = [
        project_root / ".godot" / "logs" / "editor.log",
        project_root / ".godot" / "logs" / "game.log",
        Path.home() / ".godot" / "logs" / "editor.log",
    ]

    for log_path in log_paths:
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8")
            all_lines = content.split("\n")
            return {
                "path": str(log_path),
                "lines": all_lines[-lines:],
                "total_lines": len(all_lines),
            }

    return {"error": "No log file found"}


@mcp.tool(
    name="godot_watch_files",
    annotations={
        "title": "List Watched Files",
        "description": "List files that would be monitored in the project",
    },
)
def watch_files(
    project_path: Optional[str] = None,
    watch_scenes: bool = True,
    watch_scripts: bool = True,
    watch_resources: bool = True,
) -> dict:
    """
    List scene, script, and/or resource files in the project (for monitoring or refresh).

    This server does not run a background file watcher; use the returned paths with your
    editor or call godot_refresh_project() after external changes.

    Args:
        project_path: Path to project
        watch_scenes: Include .tscn files
        watch_scripts: Include .gd files
        watch_resources: Include .tres files

    Returns:
        File counts and optional note for refresh workflow
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return {"error": bad}
    project_root = resolve_project_directory(project_path)
    if not project_root:
        return {"error": "No project found"}

    watched = []

    if watch_scenes:
        watched.extend(
            find_files_by_extension(project_root, SCENE_EXTENSIONS, relative=True)
        )
    if watch_scripts:
        watched.extend(
            find_files_by_extension(project_root, SCRIPT_EXTENSIONS, relative=True)
        )
    if watch_resources:
        watched.extend(
            find_files_by_extension(project_root, RESOURCE_EXTENSIONS, relative=True)
        )

    return {
        "status": "listed",
        "project": str(project_root),
        "watched_files": len(watched),
        "file_types": {
            "scenes": watch_scenes,
            "scripts": watch_scripts,
            "resources": watch_resources,
        },
        "note": "No background watcher runs in this server. Use godot_refresh_project() after file changes.",
    }


@mcp.tool(
    name="godot_refresh_project",
    annotations={
        "title": "Refresh Project Cache",
        "description": "Refresh cached project information",
    },
)
def refresh_project(project_path: Optional[str] = None) -> dict:
    """
    Refresh project cache after file changes.

    Args:
        project_path: Path to project

    Returns:
        Refresh status
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return {"error": bad}
    project_root = resolve_project_directory(project_path)
    if not project_root:
        return {"error": "No project found"}

    scenes = find_files_by_extension(project_root, SCENE_EXTENSIONS)
    scripts = find_files_by_extension(project_root, SCRIPT_EXTENSIONS)
    resources = find_files_by_extension(project_root, RESOURCE_EXTENSIONS)

    return {
        "status": "refreshed",
        "project": str(project_root),
        "scenes": len(scenes),
        "scripts": len(scripts),
        "resources": len(resources),
    }


@mcp.tool(
    name="godot_get_project_files",
    annotations={
        "title": "Get All Project Files",
        "description": "Get complete file listing for project",
        "readOnlyHint": True,
    },
)
def get_project_files(
    project_path: Optional[str] = None,
    include_assets: bool = True,
) -> dict:
    """
    Get all files in the project.

    Args:
        project_path: Path to project
        include_assets: Include image/audio assets

    Returns:
        Complete file listing
    """
    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return {"error": bad}
    project_root = resolve_project_directory(project_path)
    if not project_root:
        return {"error": "No project found"}

    result = {
        "project": str(project_root),
        "scenes": find_files_by_extension(project_root, SCENE_EXTENSIONS),
        "scripts": find_files_by_extension(project_root, SCRIPT_EXTENSIONS),
        "resources": find_files_by_extension(project_root, RESOURCE_EXTENSIONS),
    }

    if include_assets:
        result["assets"] = find_files_by_extension(project_root, ASSET_EXTENSIONS)

    total = len(result["scenes"]) + len(result["scripts"]) + len(result["resources"])
    if include_assets:
        total += len(result.get("assets", []))

    result["total_files"] = total

    return result


@mcp.resource("project://runtime")
def project_runtime() -> dict:
    """Get runtime information about the project."""
    godot_info = check_godot_version()
    project_root = find_project_root()

    return {
        "godot_found": godot_info.get("found", False),
        "godot_version": godot_info.get("version"),
        "project_path": str(project_root) if project_root else None,
        "mcp_workspace": str(get_mcp_workspace_root()),
    }


ASSETLIB_API = "https://godotengine.org/asset-library/api"
GITHUB_RAW = "https://raw.githubusercontent.com"


def _assetlib_search_godot_version_param(godot_version: Optional[str]) -> Optional[str]:
    """
    Map caller input to the Asset Library query param.

    The API matches godot_version narrowly (e.g. 4.0 excludes 4.4 assets). Sending \"4\" as 4.0
    hides newer listings, so major-only \"3\" / \"4\" means omit the filter (search all versions).
    Pass \"4.0\", \"4.4\", etc. to narrow.
    """
    if godot_version is None:
        return None
    v = str(godot_version).strip()
    if not v:
        return None
    if v in ("3", "4"):
        return None
    if "." not in v and v.isdigit():
        return f"{v}.0"
    return v


def _assetlib_single_asset_from_response(payload: dict) -> dict:
    """GET /asset/{id} returns a flat object; some payloads may use {\"result\": {...}}."""
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("result")
    if isinstance(inner, dict) and inner.get("asset_id") is not None:
        return inner
    if payload.get("asset_id") is not None:
        return payload
    if isinstance(inner, dict):
        return inner
    return {}


def _assetlib_zip_download_url(download_url: str) -> str:
    """Asset Library may return a site-relative path or a full https:// GitHub archive URL."""
    d = (download_url or "").strip()
    if not d:
        return ""
    if d.startswith(("http://", "https://")):
        return d
    if d.startswith("/"):
        return f"{ASSETLIB_API}{d}"
    return f"{ASSETLIB_API}/{d.lstrip('/')}"


def _promote_assetlib_addons_to_project_addons(
    project_root: Path, extract_root: Path
) -> list[str]:
    """
    GitHub zips unpack as <repo>/addons/<plugin>/plugin.cfg, but Godot resolves
    res://addons/<plugin>/ from the project root. Move each detected plugin folder
    to project_root/addons/<plugin>/ and remove the temporary extract_root tree.
    """
    import shutil

    if not extract_root.exists():
        return []

    try:
        extract_root.relative_to(project_root.resolve())
    except ValueError:
        return []

    addons_dest = project_root / "addons"
    addons_dest.mkdir(parents=True, exist_ok=True)
    promoted: list[str] = []

    for addons_dir in sorted(
        extract_root.rglob("addons"),
        key=lambda p: len(p.parts),
    ):
        if not addons_dir.is_dir() or addons_dir.name != "addons":
            continue
        try:
            subs = [p for p in addons_dir.iterdir() if p.is_dir()]
        except OSError:
            continue
        for sub in subs:
            if not (sub / "plugin.cfg").is_file():
                continue
            dest = addons_dest / sub.name
            try:
                if dest.resolve() == sub.resolve():
                    continue
            except OSError:
                pass
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(sub), str(dest))
            try:
                promoted.append(str(dest.relative_to(project_root)))
            except ValueError:
                promoted.append(str(dest))

    if promoted:
        try:
            shutil.rmtree(extract_root)
        except OSError:
            pass

    return promoted


ASSETLIB_CATEGORIES = {
    "1": "2D Tools",
    "2": "Templates",
    "3": "3D Tools",
    "4": "Plugins",
    "5": "Scripts",
    "6": "Shaders",
    "7": "GUI",
    "8": "Icon Themes",
    "9": "Tools",
    "10": "Demos",
}


@mcp.tool(
    name="godot_search_assetlib",
    annotations={
        "title": "Search Godot Asset Library",
        "description": "Search the official Godot Asset Library",
        "readOnlyHint": True,
    },
)
def search_assetlib(
    query: str,
    category: Optional[str] = None,
    godot_version: Optional[str] = None,
    sort: str = "rating",
    page: int = 1,
    asset_type: str = "addon",
) -> dict:
    """
    Search the official Godot Asset Library.

    Args:
        query: Search term
        category: Category (1=2D, 2=Templates, 3=3D, 4=Plugins, 5=Scripts, 6=Shaders, 7=GUI, 9=Tools, 10=Demos)
        godot_version: Optional. Omit for all engine versions. Use \"4.0\", \"4.4\", etc. to narrow.
            Passing only \"3\" or \"4\" does not send a filter (avoids hiding 4.4-only assets when
            searching for Godot 4).
        sort: Sort by (rating, name, updated, cost)
        page: Result page number
        asset_type: Type (addon, project, any)

    Returns:
        Search results with asset details
    """
    import urllib.parse
    import urllib.request

    params = {
        "filter": query,
        "page": page - 1,
        "sort": sort,
    }

    if asset_type and asset_type != "any":
        params["type"] = asset_type

    if category:
        if category.isdigit():
            params["category"] = category

    gv = _assetlib_search_godot_version_param(godot_version)
    if gv:
        params["godot_version"] = gv

    url = f"{ASSETLIB_API}/asset?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "GodotMCP/1.0")

        with urllib.request.urlopen(req, timeout=15) as response:
            data = response.read().decode("utf-8")

        import json

        result = json.loads(data)

        assets = []
        for item in result.get("result", [])[:20]:
            assets.append(
                {
                    "id": item.get("asset_id"),
                    "title": item.get("title"),
                    "author": item.get("author"),
                    "category": item.get("category"),
                    "godot_version": item.get("godot_version"),
                    "rating": item.get("rating"),
                    "license": item.get("cost"),
                    "support_level": item.get("support_level"),
                    "version": item.get("version_string"),
                    "modified": item.get("modify_date"),
                    "url": f"https://godotengine.org/asset-library/browser/info/{item.get('asset_id')}",
                }
            )

        return {
            "query": query,
            "count": len(assets),
            "total": result.get("total_items", 0),
            "page": page,
            "assets": assets,
        }
    except Exception as e:
        return {"error": str(e), "query": query}


@mcp.tool(
    name="godot_get_asset_info",
    annotations={
        "title": "Get Asset Details",
        "description": "Get detailed info about an asset",
        "readOnlyHint": True,
    },
)
def get_asset_info(asset_id: str) -> dict:
    """
    Get detailed information about an asset.

    Args:
        asset_id: Asset ID from the library

    Returns:
        Full asset details
    """
    import urllib.request
    import json

    aid_err = validate_asset_library_id(asset_id)
    if aid_err:
        return {"error": aid_err}

    url = f"{ASSETLIB_API}/asset/{asset_id}"

    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "GodotMCP/1.0")

        with urllib.request.urlopen(req, timeout=15) as response:
            data = response.read().decode("utf-8")

        payload = json.loads(data)
        item = _assetlib_single_asset_from_response(payload)
        if not item.get("asset_id"):
            return {"error": "Invalid or empty asset response from Asset Library API"}

        return {
            "id": item.get("asset_id"),
            "title": item.get("title"),
            "author": item.get("author"),
            "description": item.get("description"),
            "category": item.get("category"),
            "godot_version": item.get("godot_version"),
            "rating": item.get("rating"),
            "license": item.get("cost"),
            "support_level": item.get("support_level"),
            "version": item.get("version_string"),
            "download_url": item.get("download_url"),
            "browse_url": item.get("browse_url"),
            "issues_url": item.get("issues_url"),
            "modified": item.get("modify_date"),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(
    name="godot_download_asset",
    annotations={
        "title": "Download Asset",
        "description": (
            "Download and extract an Asset Library zip; promotes nested addons. "
            "User enables editor plugins in Godot — use godot_list_editor_plugins to verify."
        ),
    },
)
def download_asset(
    asset_id: str,
    project_path: Optional[str] = None,
    download_path: Optional[str] = None,
) -> dict:
    """
    Download an asset from the Asset Library.

    Zip archives are extracted under the project, then any nested .../addons/<plugin>/ (with
    plugin.cfg) is moved to project_root/addons/<plugin>/ so res://addons/<plugin>/ matches Godot.
    Editor plugins must be enabled by the user in Project Settings → Plugins (verify with
    godot_list_editor_plugins).

    Args:
        asset_id: Asset ID to download
        project_path: Target project path
        download_path: Parent folder under the project for the initial extract (often \"addons\")

    Returns:
        Download result
    """
    import shutil
    import urllib.request
    import zipfile
    import json
    import io

    bad = error_if_project_outside_workspace(project_path)
    if bad:
        return {"error": bad}
    aid_err = validate_asset_library_id(asset_id)
    if aid_err:
        return {"error": aid_err}
    project_root = resolve_project_directory(project_path)
    if not project_root:
        return {"error": "No project found"}

    get_url = f"{ASSETLIB_API}/asset/{asset_id}"

    try:
        req = urllib.request.Request(get_url)
        req.add_header("User-Agent", "GodotMCP/1.0")

        with urllib.request.urlopen(req, timeout=15) as response:
            data = response.read().decode("utf-8")

        result = json.loads(data)
        asset = _assetlib_single_asset_from_response(result)
        if not asset.get("asset_id") and not asset.get("download_url"):
            return {"error": "Invalid or empty asset response from Asset Library API"}

        download_url = asset.get("download_url")
        if not download_url:
            return {"error": "No download URL available"}

        zip_url = _assetlib_zip_download_url(str(download_url))
        if not zip_url.startswith(("http://", "https://")):
            return {"error": "Could not resolve download URL for this asset"}

        target_dir = project_root
        if download_path:
            td = safe_path(project_root, download_path)
            if not td:
                return {"error": "download_path escapes project root"}
            target_dir = td

        target_dir.mkdir(parents=True, exist_ok=True)

        zip_req = urllib.request.Request(zip_url)
        zip_req.add_header("User-Agent", "GodotMCP/1.0")

        zip_buf = bytearray()
        chunk_size = 65536
        with urllib.request.urlopen(zip_req, timeout=60) as zip_response:
            while True:
                chunk = zip_response.read(chunk_size)
                if not chunk:
                    break
                if len(zip_buf) + len(chunk) > _MAX_ZIP_DOWNLOAD_BYTES:
                    return {
                        "error": (
                            f"Zip download exceeds maximum size ({_MAX_ZIP_DOWNLOAD_BYTES} bytes)"
                        ),
                    }
                zip_buf.extend(chunk)

        zip_data = bytes(zip_buf)
        aid = str(asset.get("asset_id") or asset_id)
        zip_path = project_root / f"{aid}.zip"
        zip_path.write_bytes(zip_data)

        title_raw = str(asset.get("title") or f"asset_{asset_id}")
        folder_name = re.sub(r"[^\w\- .()]+", "_", title_raw).strip() or f"asset_{asset_id}"
        folder_name = folder_name[:200]
        extract_dir = target_dir / folder_name
        extract_dir.mkdir(parents=True, exist_ok=True)
        extract_root = extract_dir.resolve()

        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            file_infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(file_infos) > _MAX_ZIP_EXTRACT_FILES:
                zip_path.unlink(missing_ok=True)
                return {
                    "error": (
                        f"Too many files in archive (max {_MAX_ZIP_EXTRACT_FILES})"
                    ),
                }
            total_uc = 0
            for info in file_infos:
                if info.file_size > _MAX_ZIP_UNCOMPRESSED_ENTRY_BYTES:
                    zip_path.unlink(missing_ok=True)
                    return {
                        "error": (
                            f"Zip entry too large: {info.filename!r} "
                            f"(max {_MAX_ZIP_UNCOMPRESSED_ENTRY_BYTES} bytes per file)"
                        ),
                    }
                total_uc += info.file_size
                if total_uc > _MAX_ZIP_EXTRACT_TOTAL_BYTES:
                    zip_path.unlink(missing_ok=True)
                    return {
                        "error": (
                            "Total uncompressed size in zip exceeds "
                            f"limit ({_MAX_ZIP_EXTRACT_TOTAL_BYTES} bytes)"
                        ),
                    }

            for info in file_infos:
                member_rel = info.filename
                if Path(member_rel).is_absolute() or member_rel.startswith(
                    ("/", "\\")
                ):
                    zip_path.unlink(missing_ok=True)
                    return {
                        "error": f"Invalid zip entry path: {member_rel!r}",
                    }
                dest = (extract_dir / member_rel).resolve()
                try:
                    dest.relative_to(extract_root)
                except ValueError:
                    zip_path.unlink(missing_ok=True)
                    return {"error": f"Zip slip attempt: {member_rel!r}"}
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)

        promoted = _promote_assetlib_addons_to_project_addons(project_root, extract_root)

        zip_path.unlink(missing_ok=True)

        if promoted:
            extracted_files = []
            for rel in promoted:
                base = project_root / rel
                if base.is_dir():
                    extracted_files.extend(
                        str(f.relative_to(project_root))
                        for f in base.rglob("*")
                        if f.is_file()
                    )
            download_rel = promoted[0] if len(promoted) == 1 else ", ".join(promoted)
        else:
            if not extract_dir.exists():
                return {"error": "Extraction produced no files"}
            extracted_files = [
                str(f.relative_to(project_root))
                for f in extract_dir.rglob("*")
                if f.is_file()
            ]
            download_rel = str(extract_dir.relative_to(project_root))

        out = {
            "success": True,
            "asset_id": asset_id,
            "title": asset.get("title"),
            "download_path": download_rel,
            "promoted_addons": promoted,
            "files": extracted_files[:50],
            "file_count": len(extracted_files),
        }
        if promoted:
            out["note"] = (
                "Nested .../addons/<name>/ folders (with plugin.cfg) were moved to res://addons/<name>/ "
                "so res:// paths match Godot. Enable editor plugins in the Godot editor "
                "(Project Settings → Plugins); MCP does not toggle [editor_plugins]."
            )
        return out
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(
    name="godot_browse_github",
    annotations={
        "title": "Browse Godot GitHub",
        "description": "Search Godot engine repos on GitHub",
        "readOnlyHint": True,
    },
)
def browse_github(
    query: str,
    repo_type: str = "godot",
    sort: str = "stars",
) -> dict:
    """
    Search for Godot-related repositories on GitHub.

    Args:
        query: Search term
        repo_type: Type (godot, gdrextension, demo)
        sort: Sort by (stars, updated)

    Returns:
        List of repositories
    """
    import urllib.parse
    import urllib.request

    repos = {
        "godot": "godotengine/godot",
        "gdrextension": "godotengine/gdext",
        "demo": "gdquest-demos/godot-demos",
    }

    base_query = repos.get(repo_type, "godotengine/godot")
    search_url = f"https://api.github.com/search/repositories?q={urllib.parse.quote(query)}+repo:{base_query}&sort={sort}&per_page=10"

    try:
        req = urllib.request.Request(search_url)
        req.add_header("User-Agent", "GodotMCP/1.0")
        req.add_header("Accept", "application/vnd.github.v3+json")

        with urllib.request.urlopen(req, timeout=15) as response:
            data = response.read().decode("utf-8")

        import json

        result = json.loads(data)

        repos = []
        for item in result.get("items", []):
            repos.append(
                {
                    "name": item.get("full_name"),
                    "description": item.get("description"),
                    "stars": item.get("stargazers_count"),
                    "forks": item.get("forks_count"),
                    "language": item.get("language"),
                    "url": item.get("html_url"),
                    "updated": item.get("updated_at"),
                }
            )

        return {
            "query": query,
            "count": len(repos),
            "repos": repos,
        }
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    main()
