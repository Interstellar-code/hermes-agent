"""Rollback: setting discovery_mode: tool after 'both' removes load_mcp_server visibility."""
from __future__ import annotations

import pytest
from unittest.mock import patch


def test_register_tool_mode_only_registers_load_mcp_tools():
    """With discovery_mode=tool, only load_mcp_tools is registered."""
    registered = {}

    class _FakeCtx:
        def register_tool(self, name, **kw):
            registered[name] = kw
        def register_hook(self, *a, **kw):
            pass

    with patch("plugins.mcp_lazy._get_discovery_mode", return_value="tool"):
        from plugins.mcp_lazy import register
        register(_FakeCtx())

    assert "load_mcp_tools" in registered
    assert "load_mcp_server" not in registered


def test_register_server_mode_registers_both_tools():
    registered = {}

    class _FakeCtx:
        def register_tool(self, name, **kw):
            registered[name] = kw
        def register_hook(self, *a, **kw):
            pass

    with patch("plugins.mcp_lazy._get_discovery_mode", return_value="server"):
        from plugins.mcp_lazy import register
        register(_FakeCtx())

    assert "load_mcp_tools" in registered
    assert "load_mcp_server" in registered


def test_register_both_mode_registers_both_tools():
    registered = {}

    class _FakeCtx:
        def register_tool(self, name, **kw):
            registered[name] = kw
        def register_hook(self, *a, **kw):
            pass

    with patch("plugins.mcp_lazy._get_discovery_mode", return_value="both"):
        from plugins.mcp_lazy import register
        register(_FakeCtx())

    assert "load_mcp_tools" in registered
    assert "load_mcp_server" in registered


def test_register_invalid_mode_falls_back_to_tool():
    """Invalid discovery_mode config falls back to 'tool' at register time."""
    registered = {}

    class _FakeCtx:
        def register_tool(self, name, **kw):
            registered[name] = kw
        def register_hook(self, *a, **kw):
            pass

    with patch("plugins.mcp_lazy._get_discovery_mode", return_value="tool"):
        from plugins.mcp_lazy import register
        register(_FakeCtx())

    assert "load_mcp_server" not in registered
