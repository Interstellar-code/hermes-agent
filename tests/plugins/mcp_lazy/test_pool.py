"""Tests for the per-session DeferredToolPool registry."""
from __future__ import annotations

import pytest

from plugins.mcp_lazy import pool as pool_mod
from plugins.mcp_lazy.pool import DeferredToolPool, evict, get_pool


@pytest.fixture(autouse=True)
def _reset_pools():
    pool_mod._reset_for_tests()
    yield
    pool_mod._reset_for_tests()


def test_get_pool_returns_same_instance_for_same_session():
    p1 = get_pool("session-a")
    p2 = get_pool("session-a")
    assert p1 is p2


def test_get_pool_returns_distinct_instances_for_distinct_sessions():
    p1 = get_pool("session-a")
    p2 = get_pool("session-b")
    assert p1 is not p2
    assert p1.session_id == "session-a"
    assert p2.session_id == "session-b"


def test_promote_adds_to_set():
    pool = get_pool("s1")
    pool.promote("mcp_a_b")
    assert "mcp_a_b" in pool.snapshot()


def test_promote_accepts_iterable():
    pool = get_pool("s1")
    pool.promote(["mcp_a", "mcp_b"])
    snap = pool.snapshot()
    assert snap == frozenset({"mcp_a", "mcp_b"})


def test_promote_dedupes():
    pool = get_pool("s1")
    pool.promote("x")
    pool.promote("x")
    pool.promote(["x", "x"])
    assert pool.snapshot() == frozenset({"x"})


def test_promote_strips_whitespace_and_drops_empties():
    pool = get_pool("s1")
    pool.promote(["  trim_me  ", "", "  ", "ok"])
    assert pool.snapshot() == frozenset({"trim_me", "ok"})


def test_promote_ignores_non_strings():
    pool = get_pool("s1")
    pool.promote([None, 42, {"foo": "bar"}, "real"])
    assert pool.snapshot() == frozenset({"real"})


def test_session_isolation():
    pool_a = get_pool("session-a")
    pool_b = get_pool("session-b")
    pool_a.promote("tool_in_a_only")
    assert "tool_in_a_only" not in pool_b.snapshot()


def test_snapshot_is_immutable():
    pool = get_pool("s")
    pool.promote("x")
    snap = pool.snapshot()
    assert isinstance(snap, frozenset)
    # frozenset has no .add — would raise AttributeError
    with pytest.raises(AttributeError):
        snap.add("y")  # type: ignore[attr-defined]


def test_snapshot_decoupled_from_subsequent_mutations():
    pool = get_pool("s")
    pool.promote("first")
    snap = pool.snapshot()
    pool.promote("second")
    # Earlier snapshot does not see later promotions.
    assert "second" not in snap
    assert "second" in pool.snapshot()


def test_clear_drops_all_promotions():
    pool = get_pool("s")
    pool.promote(["a", "b", "c"])
    pool.clear()
    assert pool.snapshot() == frozenset()


def test_evict_removes_pool_from_registry():
    p1 = get_pool("evict-me")
    p1.promote("x")
    evict("evict-me")
    # Subsequent get_pool returns a fresh pool with no promotions.
    p2 = get_pool("evict-me")
    assert p2 is not p1
    assert p2.snapshot() == frozenset()


def test_evict_unknown_session_is_noop():
    # Should not raise.
    evict("never-existed")


def test_unattributed_session_falls_back_to_shared_pool():
    pool = get_pool("")  # empty session_id
    assert pool.session_id == "__unattributed__"


def test_pool_thread_safe_under_concurrent_promotion():
    import threading
    pool = get_pool("threaded")
    names = [f"tool_{i}" for i in range(50)]

    def worker(start, end):
        for i in range(start, end):
            pool.promote(names[i])

    threads = [
        threading.Thread(target=worker, args=(0, 25)),
        threading.Thread(target=worker, args=(25, 50)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert pool.snapshot() == frozenset(names)
