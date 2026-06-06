"""EDGE #8: parallel tool calls with mixed stub/full/non-MCP in one turn.

Tests the pre_tool_call hook (CRITICAL #1 auto-promote-single-tool flow,
registered against the existing pre_tool_call hook surface so it actually
fires during dispatch via get_pre_tool_call_block_message).
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

from plugins.mcp_lazy.pool import _current_agent_var, _reset_for_tests, get_pool
from plugins.mcp_lazy.hook_impl import pre_tool_call

_LAZY_CFG = {"lazy_loading": True}


class _Agent:
    def __init__(self, session_id="parallel-test", valid_tools=None):
        self.session_id = session_id
        self.valid_tool_names = set(valid_tools or [
            "mcp_trek_search",
            "mcp_trek_create",
            "mcp_gmail_send",
        ])
        self._mcp_lazy_pool = None


@pytest.fixture(autouse=True)
def reset_and_patch_config():
    _reset_for_tests()
    with patch("plugins.mcp_lazy.hook_impl._load_config", return_value=_LAZY_CFG):
        yield
    _reset_for_tests()


def _call(tool_name: str, agent: _Agent):
    """Invoke pre_tool_call with ContextVar agent stashed (mirrors transform_tools)."""
    token = _current_agent_var.set(agent)
    try:
        return pre_tool_call(tool_name=tool_name, args={}, session_id=agent.session_id)
    finally:
        _current_agent_var.reset(token)


def test_stub_call_returns_block_directive():
    agent = _Agent()
    result = _call("mcp_trek_search", agent)
    assert result is not None
    assert result["action"] == "block"
    assert "mcp_trek_search" in result["message"]


def test_promoted_tool_passes_through():
    """Already-promoted tool: hook returns None so dispatch proceeds."""
    agent = _Agent()
    pool = get_pool(agent.session_id)
    pool.promote(["mcp_trek_search"])
    assert _call("mcp_trek_search", agent) is None


def test_non_mcp_tool_passes_through():
    agent = _Agent()
    assert _call("terminal", agent) is None


def test_unknown_mcp_tool_passes_through():
    """Hallucinated tool name not in valid_tool_names: let dispatch surface the error."""
    agent = _Agent()
    assert _call("mcp_hallucinated_tool", agent) is None


def test_auto_promotes_to_pool():
    agent = _Agent()
    pool = get_pool(agent.session_id)
    assert not pool.is_promoted("mcp_trek_search")
    _call("mcp_trek_search", agent)
    assert pool.is_promoted("mcp_trek_search")


def test_independent_per_tool_promotion():
    """Each stub call independently promotes its own tool (EDGE #8)."""
    agent = _Agent()
    pool = get_pool(agent.session_id)

    _call("mcp_trek_search", agent)
    _call("mcp_gmail_send", agent)

    assert pool.is_promoted("mcp_trek_search")
    assert pool.is_promoted("mcp_gmail_send")
    assert not pool.is_promoted("mcp_trek_create")


def test_lazy_loading_off_returns_none():
    agent = _Agent()
    with patch("plugins.mcp_lazy.hook_impl._load_config", return_value={"lazy_loading": False}):
        assert _call("mcp_trek_search", agent) is None


def test_missing_session_id_returns_none():
    agent = _Agent(session_id="")
    assert _call("mcp_trek_search", agent) is None


def test_missing_agent_context_returns_none():
    """No agent in ContextVar (e.g. background path that didn't run transform_tools): pass through."""
    # Don't set the ContextVar — handler should return None safely.
    result = pre_tool_call(tool_name="mcp_trek_search", args={}, session_id="anysession")
    assert result is None
