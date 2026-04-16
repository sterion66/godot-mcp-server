"""Smoke tests: import and syntax sanity for the MCP server module."""

import ast
from pathlib import Path


def test_module_imports():
    import godot_mcp_server as gms

    assert hasattr(gms, "mcp")
    assert callable(gms.main)


def test_module_ast_parse():
    root = Path(__file__).resolve().parents[1]
    src = (root / "godot_mcp_server.py").read_text(encoding="utf-8")
    ast.parse(src)


def test_extract_godot_validation_messages():
    import godot_mcp_server as gms

    sample = """
    SCRIPT ERROR: Parse Error: Cannot infer the type of "road" variable.
    at: GDScript::reload (res://scripts/level_setup.gd:156)
    some unrelated line
    ERROR: Failed to load script "res://foo.gd" with error "Parse error".
    """
    messages = gms._extract_godot_validation_messages(sample)

    assert any("Cannot infer the type" in m for m in messages)
    assert any("Failed to load script" in m for m in messages)
