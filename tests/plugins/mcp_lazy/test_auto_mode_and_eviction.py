"""Tests for mcp_lazy Pattern 2 (auto mode) and Pattern 3 (idle eviction)."""
import json

import pytest

from plugins.mcp_lazy import hook_impl
from plugins.mcp_lazy.hook_impl import _lazy_mode, pre_tool_call, transform_tools
from plugins.mcp_lazy.pool import DeferredToolPool, _reset_for_tests, get_pool
from plugins.mcp_lazy.stubs import is_stub_schema


def _tool(name, desc="d", pad=0):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc + ("x" * pad),
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }


class FakeAgent:
    def __init__(self, session_id, tools):
        self.session_id = session_id
        self.tools = tools
        self.valid_tool_names = {
            t["function"]["name"] for t in tools
        }


@pytest.fixture(autouse=True)
def clean_pools():
    _reset_for_tests()
    yield
    _reset_for_tests()


# ---------------------------------------------------------------- _lazy_mode

@pytest.mark.parametrize("val,expected", [
    (True, "on"), (False, "off"), (None, "off"),
    ("on", "on"), ("true", "on"), ("auto", "auto"), ("AUTO", "auto"),
    ("off", "off"), ("false", "off"), ("", "off"),
])
def test_lazy_mode_normalization(val, expected):
    assert _lazy_mode({"lazy_loading": val}) == expected


# ---------------------------------------------------------------- pool state

def test_promote_stamps_last_used_and_evict_idle():
    pool = DeferredToolPool("s1")
    pool.promote(["mcp_a_x", "mcp_a_y"])
    for _ in range(10):
        pool.tick()
    pool.touch("mcp_a_y")  # used at turn 10

    evicted = pool.evict_idle(10)
    assert evicted == ["mcp_a_x"]
    assert pool.is_promoted("mcp_a_y")
    assert not pool.is_promoted("mcp_a_x")

    # Re-promotion works after eviction.
    pool.promote("mcp_a_x")
    assert pool.is_promoted("mcp_a_x")


def test_evict_idle_disabled_and_touch_ignores_unpromoted():
    pool = DeferredToolPool("s2")
    pool.promote("mcp_a_x")
    for _ in range(50):
        pool.tick()
    assert pool.evict_idle(0) == []
    pool.touch("mcp_never_promoted")
    assert not pool.is_promoted("mcp_never_promoted")


def test_clear_resets_eviction_state():
    pool = DeferredToolPool("s3")
    pool.promote("mcp_a_x")
    pool.tick()
    pool._lazy_active = False
    pool.clear()
    assert pool._turn == 0
    assert pool._last_used == {}
    assert pool._lazy_active is True


# -------------------------------------------------------- transform_tools

def _patch_cfg(monkeypatch, cfg):
    monkeypatch.setattr(hook_impl, "_load_config", lambda: cfg)
    monkeypatch.setattr(hook_impl, "_eligible_servers", lambda: None)
    monkeypatch.setattr(hook_impl, "_server_descriptions", lambda: {})


def test_auto_mode_passthrough_below_threshold(monkeypatch):
    tools = [_tool("mcp_srv_small")]
    agent = FakeAgent("sess-auto", tools)
    _patch_cfg(monkeypatch, {"lazy_loading": "auto", "lazy_auto_threshold_tokens": 4000})

    assert transform_tools(tools, agent=agent) is None  # pass-through
    pool = get_pool("sess-auto")
    assert pool._lazy_active is False

    # pre_tool_call must not intercept on a pass-through turn.
    assert pre_tool_call(tool_name="mcp_srv_small", session_id="sess-auto") is None
    assert not pool.is_promoted("mcp_srv_small")


def test_auto_mode_stubs_above_threshold(monkeypatch):
    tools = [_tool(f"mcp_srv_t{i}", pad=500) for i in range(20)]
    agent = FakeAgent("sess-auto2", tools)
    _patch_cfg(monkeypatch, {"lazy_loading": "auto", "lazy_auto_threshold_tokens": 100})

    result = transform_tools(tools, agent=agent)
    assert result is not None
    assert all(is_stub_schema(t) for t in result)
    assert get_pool("sess-auto2")._lazy_active is True


def test_eviction_batch_in_transform_tools(monkeypatch):
    tools = [_tool(f"mcp_srv_t{i}", pad=2000) for i in range(4)]
    agent = FakeAgent("sess-evict", tools)
    cfg = {
        "lazy_loading": True,
        "lazy_evict_idle_turns": 3,
        "lazy_evict_cost_threshold_tokens": 100,
    }
    _patch_cfg(monkeypatch, cfg)

    pool = get_pool("sess-evict")
    pool.promote([t["function"]["name"] for t in tools[:2]])

    # Turns 1-2: promoted tools stay full (not yet idle long enough).
    for _ in range(2):
        result = transform_tools(tools, agent=agent)
    full = [t for t in result if not is_stub_schema(t)]
    assert len(full) == 2

    # Turn 3: idle >= 3 turns and cost over threshold — batch evicted.
    result = transform_tools(tools, agent=agent)
    assert all(is_stub_schema(t) for t in result)
    assert pool.snapshot() == frozenset()


def test_touch_defers_eviction(monkeypatch):
    tools = [_tool("mcp_srv_used", pad=2000), _tool("mcp_srv_idle", pad=2000)]
    agent = FakeAgent("sess-touch", tools)
    _patch_cfg(monkeypatch, {
        "lazy_loading": True,
        "lazy_evict_idle_turns": 2,
        "lazy_evict_cost_threshold_tokens": 100,
    })

    pool = get_pool("sess-touch")
    pool.promote(["mcp_srv_used", "mcp_srv_idle"])

    transform_tools(tools, agent=agent)          # turn 1
    pre_tool_call(tool_name="mcp_srv_used", session_id="sess-touch")  # stamps use
    result = transform_tools(tools, agent=agent)  # turn 2 — idle one evicted

    names_full = {
        t["function"]["name"] for t in result if not is_stub_schema(t)
    }
    assert "mcp_srv_used" in names_full
    assert "mcp_srv_idle" not in names_full
