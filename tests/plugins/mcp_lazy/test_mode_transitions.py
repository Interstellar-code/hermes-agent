"""Tests for live mode transitions and Q11 behaviour."""
from __future__ import annotations

import logging
import pytest

from plugins.mcp_lazy.pool import _reset_for_tests, get_pool
from plugins.mcp_lazy.server_stubs import is_server_stub_schema, SERVER_STUB_NAME_PREFIX
from plugins.mcp_lazy.stubs import mix_full_and_stubs, is_stub_schema


def _full(name: str):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Desc",
            "parameters": {"type": "object", "properties": {}},
        },
    }


@pytest.fixture(autouse=True)
def reset():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_tool_to_both_mode_pool_state_preserved(caplog, monkeypatch):
    """Q11: switching tool→both mid-session logs WARNING; pool state preserved."""
    from plugins.mcp_lazy import hook_impl

    class _Cfg:
        def __init__(self, mode):
            self._mode = mode
        def get(self, k, default=None):
            if k == "lazy_loading":
                return True
            if k == "discovery_mode":
                return self._mode
            return default

    class _Agent:
        session_id = "mode-flip-test"
        valid_tool_names = {"mcp_trek_search"}

    tools = [_full("mcp_trek_search")]
    agent = _Agent()
    pool = get_pool(agent.session_id)

    monkey_cfg = {"lazy_loading": True, "discovery_mode": "tool"}
    monkeypatch.setattr(hook_impl, "_load_config", lambda: monkey_cfg)
    hook_impl.transform_tools(tools, agent=agent)
    assert pool._prev_mode == "tool"

    monkey_cfg["discovery_mode"] = "both"
    with caplog.at_level(logging.WARNING, logger="plugins.mcp_lazy.hook_impl"):
        hook_impl.transform_tools(tools, agent=agent)

    assert any("discovery_mode" in r.message for r in caplog.records)
    assert pool._prev_mode == "both"


def test_server_mode_output_changes_when_server_promoted():
    """After promoting trek, both mode includes tool stubs for trek's tools."""
    tools = [_full("mcp_trek_search"), _full("mcp_trek_create")]

    # Before promotion
    result_before = mix_full_and_stubs(
        tools,
        promoted_names=frozenset(),
        lazy_servers={"trek"},
        discovery_mode="both",
        promoted_servers=frozenset(),
    )
    names_before = [t["function"]["name"] for t in result_before]
    assert "mcp_trek_search" not in names_before

    # After server promotion
    result_after = mix_full_and_stubs(
        tools,
        promoted_names=frozenset(),
        lazy_servers={"trek"},
        discovery_mode="both",
        promoted_servers=frozenset({"trek"}),
    )
    names_after = [t["function"]["name"] for t in result_after]
    assert "mcp_trek_search" in names_after


def test_server_mode_ineligible_tools_pass_through():
    """Tools whose server is not in lazy_servers pass through unchanged in any mode."""
    tools = [_full("mcp_eager_server_tool")]
    result = mix_full_and_stubs(
        tools,
        promoted_names=frozenset(),
        lazy_servers={"trek"},  # eager_server not listed
        discovery_mode="server",
    )
    names = [t["function"]["name"] for t in result]
    assert "mcp_eager_server_tool" in names
    assert not any(is_server_stub_schema(t) for t in result)


def test_rollback_tool_mode_hides_server_stubs():
    """tool mode never emits server stubs, even if pool has promoted_servers."""
    pool = get_pool("rollback-test")
    pool.promote_server("trek")

    tools = [_full("mcp_trek_search")]
    result = mix_full_and_stubs(
        tools,
        promoted_names=frozenset(),
        lazy_servers={"trek"},
        discovery_mode="tool",
        promoted_servers=pool.promoted_servers_snapshot(),
    )
    assert not any(is_server_stub_schema(t) for t in result)
    # tools should be stubbed (Phase 1 behaviour)
    assert any(is_stub_schema(t) for t in result)
