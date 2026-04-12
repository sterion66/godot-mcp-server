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
