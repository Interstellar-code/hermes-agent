"""Tests for the load_mcp_server meta-tool handler."""
from __future__ import annotations

import json
import pytest

from plugins.mcp_lazy.meta_tool_server import SCHEMA, check, handler
from plugins.mcp_lazy.pool import DeferredToolPool, _reset_for_tests


class _FakeAgent:
    def __init__(self, session_id="test-session-1", valid_tools=None):
        self.session_id = session_id
        self.valid_tool_names = set(valid_tools or [
            "mcp_trek_search",
            "mcp_trek_create",
            "mcp_gmail_send",
        ])
        self._mcp_lazy_pool = None


@pytest.fixture(autouse=True)
def reset_pools():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_schema_name():
    assert SCHEMA["name"] == "load_mcp_server"


def test_check_always_true():
    assert check() is True


@pytest.mark.asyncio
async def test_handler_promotes_server():
    agent = _FakeAgent()
    result = await handler({"server_names": ["trek"]}, _agent=agent)
    data = json.loads(result)
    assert data["ok"] is True
    assert "trek" in data["promoted"]
    assert data["available_next_turn"] is True


@pytest.mark.asyncio
async def test_handler_rejects_unknown_server():
    agent = _FakeAgent()
    result = await handler({"server_names": ["nonexistent_server_xyz"]}, _agent=agent)
    data = json.loads(result)
    assert data["ok"] is True
    assert "nonexistent_server_xyz" in data["rejected"]
    assert "nonexistent_server_xyz" not in data["promoted"]


@pytest.mark.asyncio
async def test_handler_promotes_multiple_servers():
    agent = _FakeAgent()
    result = await handler({"server_names": ["trek", "gmail"]}, _agent=agent)
    data = json.loads(result)
    assert data["ok"] is True
    assert "trek" in data["promoted"]
    assert "gmail" in data["promoted"]


@pytest.mark.asyncio
async def test_handler_no_agent_returns_error():
    result = await handler({"server_names": ["trek"]})
    data = json.loads(result)
    assert data["ok"] is False
    assert "agent" in data["error"].lower()


@pytest.mark.asyncio
async def test_handler_bad_server_names_type():
    agent = _FakeAgent()
    result = await handler({"server_names": "trek"}, _agent=agent)
    data = json.loads(result)
    assert data["ok"] is False


@pytest.mark.asyncio
async def test_handler_eager_flag_accepted():
    agent = _FakeAgent()
    result = await handler({"server_names": ["trek"], "eager": True}, _agent=agent)
    data = json.loads(result)
    assert data["ok"] is True
    assert "trek" in data["promoted"]


@pytest.mark.asyncio
async def test_handler_records_server_in_pool():
    from plugins.mcp_lazy.pool import get_pool
    agent = _FakeAgent(session_id="srv-pool-test")
    await handler({"server_names": ["trek"]}, _agent=agent)
    pool = get_pool("srv-pool-test")
    assert pool.is_server_promoted("trek")


@pytest.mark.asyncio
async def test_handler_resolves_agent_from_contextvar():
    """Handler falls back to ContextVar when no _agent kwarg given."""
    from plugins.mcp_lazy.pool import _current_agent_var
    agent = _FakeAgent(session_id="ctx-var-test")
    token = _current_agent_var.set(agent)
    try:
        result = await handler({"server_names": ["trek"]})
        data = json.loads(result)
        assert data["ok"] is True
    finally:
        _current_agent_var.reset(token)
