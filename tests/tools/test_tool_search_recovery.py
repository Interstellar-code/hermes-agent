"""Tests for tool_search absolute activation floor + deferred-tool call recovery."""
import pytest

from tools import tool_search as ts
from tools.tool_search import ToolSearchConfig, should_activate
from agent import tool_executor as te

_BASE = dict(enabled="auto", threshold_pct=10.0,
             search_default_limit=5, max_search_limit=20)


# ------------------------------------------------- threshold_tokens floor

def test_floor_activates_when_pct_unreachable():
    # 2M-context model: pct threshold = 200k, deferrable 80k → dormant
    # without the floor, active with it.
    cfg = ToolSearchConfig(**_BASE, threshold_tokens=20_000)
    assert should_activate(cfg, 80_000, 2_000_000) is True


def test_floor_disabled_by_default_preserves_behavior():
    cfg = ToolSearchConfig(**_BASE, threshold_tokens=0)
    assert should_activate(cfg, 80_000, 2_000_000) is False


def test_floor_not_crossed_falls_back_to_pct():
    cfg = ToolSearchConfig(**_BASE, threshold_tokens=100_000)
    assert should_activate(cfg, 80_000, 2_000_000) is False
    assert should_activate(cfg, 250_000, 2_000_000) is True


def test_floor_ignored_when_off_or_no_deferrable():
    cfg = ToolSearchConfig(**{**_BASE, "enabled": "off"}, threshold_tokens=1)
    assert should_activate(cfg, 999_999, 2_000_000) is False
    cfg = ToolSearchConfig(**_BASE, threshold_tokens=1)
    assert should_activate(cfg, 0, 2_000_000) is False


def test_from_raw_parses_threshold_tokens():
    assert ToolSearchConfig.from_raw(
        {"enabled": "auto", "threshold_tokens": 15_000}).threshold_tokens == 15_000
    assert ToolSearchConfig.from_raw({"enabled": "auto"}).threshold_tokens == 0
    assert ToolSearchConfig.from_raw({"threshold_tokens": -5}).threshold_tokens == 0
    assert ToolSearchConfig.from_raw(True).threshold_tokens == 0
    assert ToolSearchConfig.from_raw(None).threshold_tokens == 0


# --------------------------------------- deferred_tool_recovery_message

_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "mcp_gh_create_issue",
        "description": "Create a GitHub issue.",
        "parameters": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    },
}


class FakeAgent:
    def __init__(self, valid=None):
        self.valid_tool_names = valid if valid is not None else {
            "tool_search", "tool_describe", "tool_call", "read_file",
        }
        self.enabled_toolsets = None
        self.disabled_toolsets = None


def test_recovery_message_carries_schema_and_tool_call_hint(monkeypatch):
    monkeypatch.setattr(
        te, "_tool_search_scoped_names",
        lambda a: frozenset({"mcp_gh_create_issue"}))
    import model_tools
    monkeypatch.setattr(model_tools, "get_tool_definitions",
                        lambda **kw: [_TOOL_DEF])
    monkeypatch.setattr(ts, "is_deferrable_tool_name", lambda n: True)

    msg = te.deferred_tool_recovery_message(FakeAgent(), "mcp_gh_create_issue")
    assert msg is not None
    assert "tool_call" in msg
    assert '"title"' in msg  # full parameter schema included
    assert "deferred" in msg


def test_recovery_none_when_tool_search_inactive():
    agent = FakeAgent(valid={"read_file", "terminal"})
    assert te.deferred_tool_recovery_message(agent, "mcp_gh_create_issue") is None


def test_recovery_none_for_out_of_scope_name(monkeypatch):
    monkeypatch.setattr(te, "_tool_search_scoped_names", lambda a: frozenset())
    assert te.deferred_tool_recovery_message(FakeAgent(), "no_such_tool") is None
