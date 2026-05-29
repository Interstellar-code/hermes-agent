"""Regression tests for #30: get_pool check-then-set race condition.

The old code did an unlocked _pools.get() + assignment, so two concurrent
callers could both see None and both create a pool — the second silently
clobbers the first's promotions.  Fix: double-checked locking under
_strong_recent_lock so only one pool is ever created per session_id.
"""
from __future__ import annotations

import threading

import pytest

from plugins.mcp_lazy import pool as pool_mod
from plugins.mcp_lazy.pool import get_pool, evict


@pytest.fixture(autouse=True)
def _reset():
    pool_mod._reset_for_tests()
    yield
    pool_mod._reset_for_tests()


def test_concurrent_get_pool_returns_same_instance():
    """All threads racing on the same session_id must receive the same pool."""
    results: list = []
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()  # all threads start simultaneously
        results.append(get_pool("race-session"))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 20
    # Every thread must have gotten the exact same object.
    first = results[0]
    for p in results[1:]:
        assert p is first, "get_pool returned different instances for the same session_id under concurrency"


def test_concurrent_get_pool_promotions_not_lost():
    """Promotions made by one thread must be visible to all others."""
    pool_mod._reset_for_tests()
    barrier = threading.Barrier(10)
    errors: list = []

    def worker(i: int):
        barrier.wait()
        p = get_pool("promo-race")
        p.promote(f"tool_{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = get_pool("promo-race").snapshot()
    for i in range(10):
        assert f"tool_{i}" in final, f"tool_{i} lost — pool clobbered under concurrency"


def test_get_pool_idempotent_after_concurrent_creation():
    """After concurrent creation stabilises, get_pool must be idempotent."""
    pool1 = get_pool("stable")
    pool2 = get_pool("stable")
    pool3 = get_pool("stable")
    assert pool1 is pool2 is pool3
