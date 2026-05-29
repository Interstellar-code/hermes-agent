"""Regression tests for #27: server named 'server' collides with mcp_server_ prefix.

A real MCP server named "server" has tools like ``mcp_server_list``.  The
synthetic discovery stub for that server is also named ``mcp_server_server``
(prefix + sanitised name).  The old code used the name prefix to detect stubs,
which is ambiguous.  Fix: use ``_IS_SERVER_STUB_KEY`` sentinel; also confirm
that pre_tool_call treats ``mcp_server_list`` as a real tool (in valid_tool_names)
and falls through to per-tool auto-promote rather than treating it as a stub.
"""
from __future__ import annotations

import pytest

from plugins.mcp_lazy import pool as pool_mod
from plugins.mcp_lazy.pool import get_pool, evict
from plugins.mcp_lazy.server_stubs import (
    make_server_stub_schema,
    is_server_stub_schema,
    _IS_SERVER_STUB_KEY,
)


@pytest.fixture(autouse=True)
def _reset():
    pool_mod._reset_for_tests()
    yield
    pool_mod._reset_for_tests()


# ---------------------------------------------------------------------------
# Sentinel-based detection (#27)
# ---------------------------------------------------------------------------

def test_is_server_stub_schema_uses_sentinel_not_name():
    """Schemas built by make_server_stub_schema carry the sentinel key."""
    stub = make_server_stub_schema("server", "desc", tool_count=3)
    assert stub.get(_IS_SERVER_STUB_KEY) is True
    assert is_server_stub_schema(stub)


def test_real_tool_named_mcp_server_list_is_not_stub():
    """A hand-crafted schema for a real tool mcp_server_list has no sentinel."""
    real_tool = {
        "type": "function",
        "function": {
            "name": "mcp_server_list",
            "description": "List items on the server",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }
    assert not is_server_stub_schema(real_tool)


def test_server_named_server_stub_is_detected():
    """Stub for a server literally named 'server' is still detected as a stub."""
    stub = make_server_stub_schema("server", "The server MCP server", tool_count=2)
    # Name will be mcp_server_server — same prefix as a tool on 'server' server.
    assert stub["function"]["name"] == "mcp_server_server"
    assert is_server_stub_schema(stub)


# ---------------------------------------------------------------------------
# pre_tool_call: real tool from 'server' server falls through, not blocked (#27)
# ---------------------------------------------------------------------------

def test_pre_tool_call_real_tool_from_server_named_server_not_blocked(monkeypatch):
    """mcp_server_list is a real tool in valid_tool_names — must not be blocked as stub."""
    import plugins.mcp_lazy.hook_impl as hi

    monkeypatch.setattr(
        hi, "_load_config",
        lambda: {"lazy_loading": True, "discovery_mode": "tool"},
    )

    class FakeAgent:
        session_id = "sess-server"
        valid_tool_names = {"mcp_server_list", "mcp_server_create"}
        _mcp_lazy_pool = None

    agent = FakeAgent()
    pool = get_pool("sess-server")
    hi._current_agent_var.set(agent)

    # mcp_server_list is a real (unpromoted) tool — should get auto-promoted
    # and return a block-with-retry, NOT a "use load_mcp_server" block.
    result = hi.pre_tool_call(
        tool_name="mcp_server_list",
        args={},
        session_id="sess-server",
    )
    assert result is not None
    assert result.get("action") == "block"
    # Should be the auto-promote message, not the discovery-stub message
    assert "load_mcp_server" not in result["message"]
    assert "stub" in result["message"].lower()
    assert pool.is_promoted("mcp_server_list")


def test_pre_tool_call_discovery_stub_for_server_named_server_blocked(monkeypatch):
    """mcp_server_server is the discovery stub name; not in valid_tool_names → load_mcp_server block."""
    import plugins.mcp_lazy.hook_impl as hi

    monkeypatch.setattr(
        hi, "_load_config",
        lambda: {"lazy_loading": True, "discovery_mode": "server"},
    )

    class FakeAgent:
        session_id = "sess-server2"
        # Only real tools are here; the stub itself is NOT a valid tool
        valid_tool_names = {"mcp_server_list", "mcp_server_create"}
        _mcp_lazy_pool = None

    agent = FakeAgent()
    hi._current_agent_var.set(agent)

    result = hi.pre_tool_call(
        tool_name="mcp_server_server",  # discovery stub name for server 'server'
        args={},
        session_id="sess-server2",
    )
    assert result is not None
    assert result.get("action") == "block"
    assert "load_mcp_server" in result["message"]
    # Must NOT have polluted the pool
    pool = get_pool("sess-server2")
    assert not pool.is_promoted("mcp_server_server")
