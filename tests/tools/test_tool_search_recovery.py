"""Tests for tool_search absolute activation floor + deferred-tool call recovery."""
import pytest

from tools import tool_search as ts
from tools.tool_search import ToolSearchConfig, should_activate
from agent import tool_executor as te

_BASE = dict(enabled="auto", threshold_pct=10.0,
             search_default_limit=5, max_search_limit=20)


# ------------------------------------------------- threshold_tokens floor

def test_auto_activates_whenever_anything_is_deferrable():
    """The guarantee that REPLACED the old threshold_tokens floor.

    The floor existed because a percentage threshold is unreachable on huge
    contexts (10% of 2M = 200k), which left "auto" permanently dormant however
    large the deferrable surface got. should_activate no longer consults any
    threshold: any deferrable token activates. That dormancy is now structurally
    impossible, so the floor is subsumed rather than lost -- and the config key
    it was configured with has been removed instead of left inert.
    """
    cfg = ToolSearchConfig(**_BASE)
    assert should_activate(cfg, 80_000, 2_000_000) is True    # was dormant pre-floor
    assert should_activate(cfg, 1, 2_000_000) is True
    assert should_activate(cfg, 1, None) is True              # unknown context


def test_no_activation_when_off_or_nothing_deferrable():
    assert should_activate(ToolSearchConfig(**{**_BASE, "enabled": "off"}),
                           999_999, 2_000_000) is False
    assert should_activate(ToolSearchConfig(**_BASE), 0, 2_000_000) is False


def test_threshold_tokens_key_is_gone_not_silently_ignored():
    """A removed key must not resurface as a dead attribute nothing reads."""
    assert not hasattr(ToolSearchConfig(**_BASE), "threshold_tokens")
    assert not hasattr(ToolSearchConfig.from_raw({"threshold_tokens": 15_000}),
                       "threshold_tokens")   # unknown keys stay ignored, no crash


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
    monkeypatch.setattr(ts, "is_deferrable_tool_name", lambda n, d=None: True)

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


def test_recovery_resolves_tools_in_both_defer_list_and_core_set(monkeypatch):
    """Regression: the recovery path must use the SAME defer set the producer
    used to hide the tool.

    ``is_deferrable_tool_name(name)`` without ``defer_tools`` skips the
    defer-list branch and falls through to the core-set check, so every tool in
    BOTH _DEFAULT_DEFERRED_TOOLS and _HERMES_CORE_TOOLS answered "not
    deferrable" here while the assembly had already hidden it. The model would
    see such a tool deferred, call it by name, and get an unknown-tool error
    instead of an executable recovery message.

    Deliberately does NOT stub is_deferrable_tool_name -- the real one is what
    regressed.
    """
    both = sorted(set(ts._DEFAULT_DEFERRED_TOOLS) & set(ts._core_tool_names()))
    assert both, "fixture assumes the two lists overlap"
    name = both[0]

    monkeypatch.setattr(te, "_tool_search_scoped_names", lambda a: frozenset({name}))
    import model_tools
    monkeypatch.setattr(
        model_tools, "get_tool_definitions",
        lambda **kw: [{"type": "function", "function": {
            "name": name, "description": "d",
            "parameters": {"type": "object", "properties": {}}}}])

    msg = te.deferred_tool_recovery_message(FakeAgent(), name)
    assert msg is not None, f"{name} is deferred by the producer but unresolvable here"
    assert name in msg
