"""Regression tests for #29: _prev_mode cleared on pool evict.

The old code stored discovery_mode in a module-level dict in hook_impl keyed
by session_id.  evict() never cleared it, so stale entries accumulated and
could cause false mid-session-flip warnings on new sessions that reused an
old session_id.  Fix: move _prev_mode into DeferredToolPool._prev_mode so it
is cleared automatically when evict() calls pool.clear().
"""
from __future__ import annotations

import pytest

from plugins.mcp_lazy import pool as pool_mod
from plugins.mcp_lazy.pool import DeferredToolPool, get_pool, evict


@pytest.fixture(autouse=True)
def _reset():
    pool_mod._reset_for_tests()
    yield
    pool_mod._reset_for_tests()


def test_prev_mode_starts_none():
    pool = get_pool("s1")
    assert pool._prev_mode is None


def test_prev_mode_set_and_read():
    pool = get_pool("s1")
    pool._prev_mode = "tool"
    assert pool._prev_mode == "tool"


def test_prev_mode_cleared_on_evict():
    """After evict(), a new pool for the same session_id has _prev_mode=None."""
    pool1 = get_pool("sess-evict")
    pool1._prev_mode = "server"

    evict("sess-evict")

    pool2 = get_pool("sess-evict")
    # Must be a fresh pool with no stale mode.
    assert pool2 is not pool1
    assert pool2._prev_mode is None


def test_prev_mode_cleared_on_pool_clear():
    pool = get_pool("sess-clear")
    pool._prev_mode = "both"
    pool.clear()
    assert pool._prev_mode is None


def test_no_false_flip_warning_after_evict_and_reuse(monkeypatch):
    """Reusing a session_id after evict must not log a spurious flip warning."""
    import logging
    import plugins.mcp_lazy.hook_impl as hi

    monkeypatch.setattr(
        hi, "_load_config",
        lambda: {"lazy_loading": True, "discovery_mode": "tool"},
    )
    monkeypatch.setattr(hi, "_eligible_servers", lambda: None)
    monkeypatch.setattr(hi, "_server_descriptions", lambda: {})

    class FakeAgent:
        session_id = "reused-session"
        valid_tool_names: set = set()
        _mcp_lazy_pool = None

    agent = FakeAgent()
    hi._current_agent_var.set(agent)

    # First use — mode is 'tool'.
    hi.transform_tools([], agent=agent)
    pool = get_pool("reused-session")
    assert pool._prev_mode == "tool"

    # Evict (simulate session end).
    evict("reused-session")

    # Reuse the same session_id — a fresh pool is created.
    # Changing mode now must NOT trigger a flip warning.
    monkeypatch.setattr(
        hi, "_load_config",
        lambda: {"lazy_loading": True, "discovery_mode": "server"},
    )

    warned = []
    original_warning = hi.logger.warning

    def capture_warning(msg, *a, **kw):
        warned.append(msg % a if a else msg)
        original_warning(msg, *a, **kw)

    monkeypatch.setattr(hi.logger, "warning", capture_warning)

    hi.transform_tools([], agent=agent)

    flip_warnings = [w for w in warned if "discovery_mode changed mid-session" in w]
    assert flip_warnings == [], f"Unexpected flip warning after evict+reuse: {flip_warnings}"
