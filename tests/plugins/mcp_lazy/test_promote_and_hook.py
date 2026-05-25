"""End-to-end-ish tests for promote, transform_tools hook, and the
``mcp_load_tools`` meta-tool. These don't spin up a real Hermes
agent — they use a light SimpleNamespace stand-in with just the
attributes the plugin actually touches.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plugins.mcp_lazy import hook_impl, meta_tool, pool as pool_mod
from plugins.mcp_lazy.promote import promote_tools
from plugins.mcp_lazy.stubs import is_stub_schema


@pytest.fixture(autouse=True)
def _reset_pools():
    pool_mod._reset_for_tests()
    yield
    pool_mod._reset_for_tests()


def _fake_agent(session_id="s1", valid_names=None):
    return SimpleNamespace(
        session_id=session_id,
        valid_tool_names=valid_names or set(),
    )


def _full(name: str):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"description for {name}",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }


# -- promote_tools ------------------------------------------------------------

def test_promote_tools_records_in_pool():
    agent = _fake_agent("s1", {"mcp_a_b"})
    accepted = promote_tools(agent, ["mcp_a_b"])
    assert accepted == ["mcp_a_b"]
    pool = pool_mod.get_pool("s1")
    assert "mcp_a_b" in pool.snapshot()


def test_promote_tools_drops_unknown_names():
    agent = _fake_agent("s1", {"mcp_a_b"})
    accepted = promote_tools(agent, ["mcp_a_b", "mcp_nope_x"])
    assert accepted == ["mcp_a_b"]
    pool = pool_mod.get_pool("s1")
    assert "mcp_nope_x" not in pool.snapshot()


def test_promote_tools_accepts_without_valid_names():
    agent = SimpleNamespace(session_id="s1")  # no valid_tool_names attr
    accepted = promote_tools(agent, ["mcp_anything"])
    # When valid_tool_names is absent/empty we accept everything.
    assert accepted == ["mcp_anything"]


def test_promote_tools_skips_non_strings_and_whitespace():
    agent = _fake_agent("s1", {"real"})
    accepted = promote_tools(agent, ["", "  ", None, 42, "real"])  # type: ignore[list-item]
    assert accepted == ["real"]


# -- transform_tools hook -----------------------------------------------------

def test_transform_tools_no_op_when_lazy_disabled():
    with patch.object(hook_impl, "_load_config", return_value={"lazy_loading": False}):
        agent = _fake_agent("s1")
        out = hook_impl.transform_tools([_full("mcp_a_b")], agent=agent)
        assert out is None


def test_transform_tools_no_op_when_no_session_id():
    with patch.object(hook_impl, "_load_config", return_value={"lazy_loading": True}):
        agent = SimpleNamespace()  # no session_id
        out = hook_impl.transform_tools([_full("mcp_a_b")], agent=agent)
        assert out is None


def test_transform_tools_stubs_mcp_tools():
    cfg = {"lazy_loading": True, "lazy_stub_max_desc": 50}
    with patch.object(hook_impl, "_load_config", return_value=cfg), \
         patch("hermes_cli.config.load_config", return_value={"mcp_servers": {}}):
        agent = _fake_agent("s1")
        out = hook_impl.transform_tools([_full("mcp_a_b"), _full("terminal")], agent=agent)
        assert out is not None
        assert is_stub_schema(out[0])
        assert not is_stub_schema(out[1])


def test_transform_tools_keeps_promoted_full():
    cfg = {"lazy_loading": True}
    with patch.object(hook_impl, "_load_config", return_value=cfg), \
         patch("hermes_cli.config.load_config", return_value={"mcp_servers": {}}):
        agent = _fake_agent("s1")
        promote_tools(agent, ["mcp_a_b"])
        out = hook_impl.transform_tools([_full("mcp_a_b"), _full("mcp_c_d")], agent=agent)
        assert not is_stub_schema(out[0])  # promoted
        assert is_stub_schema(out[1])      # not promoted


def test_transform_tools_per_server_opt_out():
    cfg = {"lazy_loading": True}
    mcp_cfg = {"mcp_servers": {"gmail": {"lazy": False}, "trek": {}}}
    with patch.object(hook_impl, "_load_config", return_value=cfg), \
         patch("hermes_cli.config.load_config", return_value=mcp_cfg):
        agent = _fake_agent("s1")
        out = hook_impl.transform_tools(
            [_full("mcp_gmail_send"), _full("mcp_trek_search")],
            agent=agent,
        )
        # gmail opts out → full; trek → stubbed
        assert not is_stub_schema(out[0])
        assert is_stub_schema(out[1])


def test_transform_tools_isolates_sessions():
    cfg = {"lazy_loading": True}
    with patch.object(hook_impl, "_load_config", return_value=cfg), \
         patch("hermes_cli.config.load_config", return_value={"mcp_servers": {}}):
        agent_a = _fake_agent("s_a", {"mcp_x"})
        agent_b = _fake_agent("s_b", {"mcp_x"})
        promote_tools(agent_a, ["mcp_x"])

        out_a = hook_impl.transform_tools([_full("mcp_x")], agent=agent_a)
        out_b = hook_impl.transform_tools([_full("mcp_x")], agent=agent_b)

        assert not is_stub_schema(out_a[0])  # a promoted it
        assert is_stub_schema(out_b[0])      # b did not


def test_transform_tools_swallows_internal_errors():
    # Force _load_config to blow up → hook must return None, not raise.
    with patch.object(hook_impl, "_load_config", side_effect=RuntimeError("boom")):
        out = hook_impl.transform_tools([_full("mcp_a")], agent=_fake_agent())
        assert out is None


def test_transform_tools_sets_contextvar():
    cfg = {"lazy_loading": True}
    with patch.object(hook_impl, "_load_config", return_value=cfg), \
         patch("hermes_cli.config.load_config", return_value={"mcp_servers": {}}):
        agent = _fake_agent("s_ctx")
        hook_impl.transform_tools([_full("mcp_a")], agent=agent)
        assert pool_mod._current_agent_var.get() is agent


# -- mcp_load_tools meta-tool -------------------------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_meta_tool_accepts_via_kwargs():
    agent = _fake_agent("s_meta", {"mcp_x_y"})
    out = json.loads(_run(meta_tool.handler({"tool_names": ["mcp_x_y"]}, _agent=agent)))
    assert out["ok"] is True
    assert out["promoted"] == ["mcp_x_y"]
    assert out["rejected"] == []


def test_meta_tool_falls_back_to_contextvar():
    agent = _fake_agent("s_meta_ctx", {"mcp_x_y"})
    pool_mod._current_agent_var.set(agent)
    out = json.loads(_run(meta_tool.handler({"tool_names": ["mcp_x_y"]})))
    assert out["ok"] is True
    assert out["promoted"] == ["mcp_x_y"]


def test_meta_tool_reports_rejected_unknown_names():
    agent = _fake_agent("s_meta_rej", {"mcp_known"})
    out = json.loads(_run(meta_tool.handler(
        {"tool_names": ["mcp_known", "mcp_typo"]},
        _agent=agent,
    )))
    assert "mcp_typo" in out["rejected"]
    assert "mcp_known" in out["promoted"]


def test_meta_tool_rejects_non_list_tool_names():
    agent = _fake_agent("s")
    out = json.loads(_run(meta_tool.handler({"tool_names": "not-a-list"}, _agent=agent)))
    assert out["ok"] is False
    assert "must be an array" in out["error"]


def test_meta_tool_fails_when_no_agent():
    # No agent in kwargs and ContextVar not set.
    pool_mod._current_agent_var.set(None)
    out = json.loads(_run(meta_tool.handler({"tool_names": ["x"]})))
    assert out["ok"] is False
    assert "agent context unavailable" in out["error"]
