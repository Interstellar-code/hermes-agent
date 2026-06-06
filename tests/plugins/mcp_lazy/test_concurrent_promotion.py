"""Concurrent server promotion: two threads promote same server simultaneously."""
from __future__ import annotations

import threading

import pytest

from plugins.mcp_lazy.pool import DeferredToolPool, _reset_for_tests, get_pool


@pytest.fixture(autouse=True)
def reset():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_concurrent_promote_server_idempotent():
    """Two threads promoting the same server leaves pool in consistent state."""
    pool = get_pool("concurrent-test")
    errors = []

    def promote():
        try:
            pool.promote_server("trek", eager=False)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=promote) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert pool.is_server_promoted("trek")
    # Idempotent — only one entry in the set.
    assert pool.promoted_servers_snapshot() == frozenset({"trek"})


def test_concurrent_tool_and_server_promotion():
    """Tool and server promotion simultaneously don't corrupt each other."""
    pool = get_pool("mixed-concurrent-test")
    errors = []

    def promote_tool():
        try:
            pool.promote(["mcp_trek_search", "mcp_gmail_send"])
        except Exception as exc:
            errors.append(exc)

    def promote_server():
        try:
            pool.promote_server("trek", eager=False)
        except Exception as exc:
            errors.append(exc)

    threads = (
        [threading.Thread(target=promote_tool) for _ in range(5)] +
        [threading.Thread(target=promote_server) for _ in range(5)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert pool.is_server_promoted("trek")
    assert "mcp_trek_search" in pool.snapshot()


def test_promote_server_eager_flag_is_idempotent_without_warning(caplog):
    """The eager flag is handled by promote_server_tools, not persisted in the pool."""
    import logging
    pool = get_pool("last-writer-test")
    with caplog.at_level(logging.WARNING, logger="plugins.mcp_lazy.pool"):
        pool.promote_server("trek", eager=False)
        pool.promote_server("trek", eager=True)

    assert pool.is_server_promoted("trek")
    assert not caplog.records


def test_clear_servers_drops_all():
    pool = get_pool("clear-servers-test")
    pool.promote_server("trek")
    pool.promote_server("gmail")
    pool.clear_servers()
    assert pool.promoted_servers_snapshot() == frozenset()


def test_clear_drops_both_tools_and_servers():
    pool = get_pool("clear-all-test")
    pool.promote(["mcp_trek_search"])
    pool.promote_server("trek")
    pool.clear()
    assert pool.snapshot() == frozenset()
    assert pool.promoted_servers_snapshot() == frozenset()
