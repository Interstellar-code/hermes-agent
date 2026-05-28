"""Regression for Interstellar-code/hermes-agent#18.

After ``load_mcp_server`` promotes a server, the ``mcp_server_<name>`` discovery
stub must be retired from the tool list. Keeping the stub visible alongside
the concrete tools (``mcp_<server>_<tool>``) was reinforcing a routing loop:
the model kept calling the stub even when the promoted tools were available.

Tests cover the two layers of the fix:

* ``mix_full_and_stubs`` must omit the discovery stub for promoted servers
  while still emitting either tool stubs or full tool schemas.
* ``pre_tool_call`` must reject a stale ``mcp_server_<name>`` invocation for a
  server that is already promoted and point the model at the concrete tools.
"""
from __future__ import annotations

import pytest

from plugins.mcp_lazy import hook_impl
from plugins.mcp_lazy.pool import get_pool
from plugins.mcp_lazy.server_stubs import (
    SERVER_STUB_NAME_PREFIX,
    is_server_stub_schema,
)
from plugins.mcp_lazy.stubs import (
    is_mcp_tool,
    is_stub_schema,
    mix_full_and_stubs,
)


SERVER = "lifeplan42"


def _full(name: str, description: str = "A real tool"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
        },
    }


def _server_tools() -> list:
    return [
        _full(f"mcp_{SERVER}_list_expenses"),
        _full(f"mcp_{SERVER}_list_purchases"),
        _full(f"mcp_{SERVER}_list_income"),
    ]


# --------------------------------------------------------------------- mix


def test_mix_hides_server_stub_when_server_promoted():
    tools = _server_tools()
    promoted = {f"mcp_{SERVER}_list_expenses"}
    result = mix_full_and_stubs(
        tools,
        promoted_names=promoted,
        lazy_servers={SERVER},
        discovery_mode="both",
        promoted_servers=frozenset({SERVER}),
    )
    names = [t["function"]["name"] for t in result]
    stub_name = f"{SERVER_STUB_NAME_PREFIX}{SERVER}"
    assert stub_name not in names, (
        "promoted server should not keep its discovery stub in the tool list"
    )
    assert not any(is_server_stub_schema(t) for t in result), (
        "no server stub schema should remain after promotion"
    )

    # The promoted tool comes through full, the rest stay as per-tool stubs.
    assert f"mcp_{SERVER}_list_expenses" in names
    assert any(
        is_stub_schema(t) and t["function"]["name"] == f"mcp_{SERVER}_list_purchases"
        for t in result
    )


def test_mix_keeps_server_stub_when_server_not_promoted():
    tools = _server_tools()
    result = mix_full_and_stubs(
        tools,
        promoted_names=set(),
        lazy_servers={SERVER},
        discovery_mode="both",
        promoted_servers=frozenset(),
    )
    names = [t["function"]["name"] for t in result]
    stub_name = f"{SERVER_STUB_NAME_PREFIX}{SERVER}"
    assert stub_name in names, (
        "unpromoted server must still expose its discovery stub"
    )


def test_mix_server_mode_unaffected_for_unpromoted():
    tools = _server_tools()
    result = mix_full_and_stubs(
        tools,
        promoted_names=set(),
        lazy_servers={SERVER},
        discovery_mode="server",
        promoted_servers=frozenset(),
    )
    server_stub = [t for t in result if is_server_stub_schema(t)]
    tool_stubs = [t for t in result if is_mcp_tool(t) and not is_server_stub_schema(t)]
    assert len(server_stub) == 1
    assert tool_stubs == [], (
        "server-only mode must not leak per-tool entries for unpromoted servers"
    )


# --------------------------------------------------------------------- pre_tool_call


class _StubAgent:
    def __init__(self, tool_names):
        self.valid_tool_names = set(tool_names)


def test_pre_tool_call_rejects_stale_server_stub_after_promotion(monkeypatch):
    session_id = "test-session-issue-18"
    pool = get_pool(session_id)
    pool.clear()
    pool.promote_server(SERVER)

    agent = _StubAgent(
        [
            f"mcp_{SERVER}_list_expenses",
            f"mcp_{SERVER}_list_purchases",
            f"mcp_{SERVER}_list_income",
        ]
    )
    token = hook_impl._current_agent_var.set(agent)
    try:
        monkeypatch.setattr(hook_impl, "_load_config", lambda: {"lazy_loading": True})
        result = hook_impl.pre_tool_call(
            tool_name=f"{SERVER_STUB_NAME_PREFIX}{SERVER}",
            args={},
            session_id=session_id,
        )
    finally:
        hook_impl._current_agent_var.reset(token)
        pool.clear()

    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "already-promoted" in result["message"]
    assert f"mcp_{SERVER}_list_expenses" in result["message"]


def test_pre_tool_call_passes_through_unpromoted_server_stub(monkeypatch):
    session_id = "test-session-issue-18-unpromoted"
    pool = get_pool(session_id)
    pool.clear()

    agent = _StubAgent([])
    token = hook_impl._current_agent_var.set(agent)
    try:
        monkeypatch.setattr(hook_impl, "_load_config", lambda: {"lazy_loading": True})
        result = hook_impl.pre_tool_call(
            tool_name=f"{SERVER_STUB_NAME_PREFIX}{SERVER}",
            args={},
            session_id=session_id,
        )
    finally:
        hook_impl._current_agent_var.reset(token)
        pool.clear()

    # Server not promoted → no early reject; the function should not return a
    # block dict (it falls through to normal dispatch / None).
    assert result is None or result.get("action") != "block"
