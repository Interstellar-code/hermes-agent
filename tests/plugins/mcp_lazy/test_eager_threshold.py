"""Q1: server_eager_token_threshold gates eager promotion.

Verifies promote_server_tools degrades eager=True to eager=False when
the server's full-schema token cost exceeds the configured threshold,
and that eager=True actually promotes tools to full schemas when the
threshold is not breached.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from plugins.mcp_lazy.pool import _reset_for_tests, get_pool
from plugins.mcp_lazy.promote import promote_server_tools


def _make_tool(name: str, desc_chars: int = 100) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "x" * desc_chars,
            "parameters": {
                "type": "object",
                "properties": {"p": {"type": "string", "description": "x" * desc_chars}},
            },
        },
    }


class _Agent:
    def __init__(self, session_id="eager-test", tools=None, valid_tools=None):
        self.session_id = session_id
        self.tools = tools or []
        self.valid_tool_names = set(valid_tools or [t["function"]["name"] for t in self.tools])


@pytest.fixture(autouse=True)
def reset():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_eager_under_threshold_promotes_tools_full():
    """Small server (low total cost) keeps eager=True and tools become full."""
    tools = [_make_tool("mcp_tiny_a", 20), _make_tool("mcp_tiny_b", 20)]
    agent = _Agent(tools=tools)
    with patch("plugins.mcp_lazy.promote._load_eager_threshold", return_value=10000):
        promote_server_tools(agent, ["tiny"], eager=True)
    pool = get_pool(agent.session_id)
    assert pool.is_server_promoted("tiny")
    # Eager promotion: each tool now in _promoted (will render full schema).
    assert pool.is_promoted("mcp_tiny_a")
    assert pool.is_promoted("mcp_tiny_b")


def test_eager_over_threshold_degrades_to_tool_stubs():
    """Big server (high total cost) silently degrades — tools NOT in _promoted."""
    # Each tool ~250 chars / 4 ≈ 60 tok, but make params large.
    tools = [_make_tool(f"mcp_big_t{i}", 500) for i in range(10)]
    agent = _Agent(tools=tools)
    with patch("plugins.mcp_lazy.promote._load_eager_threshold", return_value=500):
        promote_server_tools(agent, ["big"], eager=True)
    pool = get_pool(agent.session_id)
    # Server still marked as promoted for discovery layer.
    assert pool.is_server_promoted("big")
    # But individual tools NOT promoted to full schema (eager downgraded).
    assert not pool.is_promoted("mcp_big_t0")
    assert not pool.is_promoted("mcp_big_t5")


def test_non_eager_request_ignores_threshold():
    """eager=False bypasses the threshold check entirely (no cost computation)."""
    tools = [_make_tool(f"mcp_big_t{i}", 500) for i in range(10)]
    agent = _Agent(tools=tools)
    with patch("plugins.mcp_lazy.promote._load_eager_threshold", return_value=100) as mock_threshold:
        promote_server_tools(agent, ["big"], eager=False)
        mock_threshold.assert_not_called()
    pool = get_pool(agent.session_id)
    assert pool.is_server_promoted("big")
    # Tool stubs only — none promoted to full schema.
    assert not pool.is_promoted("mcp_big_t0")


def test_threshold_default_is_1500():
    """When config is absent, default threshold is 1500 (per plan Q9)."""
    from plugins.mcp_lazy.promote import _load_eager_threshold

    with patch("hermes_cli.config.load_config", return_value={}):
        assert _load_eager_threshold() == 1500
