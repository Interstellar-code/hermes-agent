"""Tests for plugins/a2a_fleet/context_store.py (Step 3, v0.2)."""
from __future__ import annotations

import asyncio
import threading
from typing import List

import pytest


def _fresh_store(max_turns: int = 20, max_contexts: int = 500):
    from a2a_fleet.context_store import ContextStore
    return ContextStore(max_turns=max_turns, max_contexts=max_contexts)


# ---------------------------------------------------------------------------
# Basic append / history
# ---------------------------------------------------------------------------

def test_append_and_history_roundtrip():
    store = _fresh_store()
    ctx = store.generate_context_id()
    store.append(ctx, "user", "hello")
    store.append(ctx, "agent", "world")
    turns = store.history(ctx)
    assert len(turns) == 2
    assert turns[0] == {"role": "user", "text": "hello"}
    assert turns[1] == {"role": "agent", "text": "world"}


def test_history_ordering():
    store = _fresh_store()
    ctx = store.generate_context_id()
    for i in range(5):
        store.append(ctx, "user", str(i))
    turns = store.history(ctx)
    assert [t["text"] for t in turns] == ["0", "1", "2", "3", "4"]


def test_history_unknown_context_returns_empty():
    store = _fresh_store()
    assert store.history("nonexistent-ctx") == []


# ---------------------------------------------------------------------------
# Generated id
# ---------------------------------------------------------------------------

def test_generate_context_id_is_uuid_like():
    store = _fresh_store()
    import uuid
    id1 = store.generate_context_id()
    id2 = store.generate_context_id()
    assert id1 != id2
    # Should parse as valid UUID
    uuid.UUID(id1)
    uuid.UUID(id2)


def test_generate_context_id_surfaced_via_module():
    from a2a_fleet import context_store
    id1 = context_store.generate_context_id()
    id2 = context_store.generate_context_id()
    assert id1 != id2


# ---------------------------------------------------------------------------
# Turn bound (max_turns)
# ---------------------------------------------------------------------------

def test_history_bound_prunes_oldest():
    store = _fresh_store(max_turns=3)
    ctx = store.generate_context_id()
    for i in range(6):
        store.append(ctx, "user", str(i))
    turns = store.history(ctx)
    assert len(turns) == 3
    assert [t["text"] for t in turns] == ["3", "4", "5"]


def test_history_limit_parameter():
    store = _fresh_store()
    ctx = store.generate_context_id()
    for i in range(10):
        store.append(ctx, "user", str(i))
    turns = store.history(ctx, limit=3)
    assert len(turns) == 3
    assert turns[-1]["text"] == "9"


# ---------------------------------------------------------------------------
# LRU context eviction
# ---------------------------------------------------------------------------

def test_lru_evicts_cold_context():
    store = _fresh_store(max_contexts=3)
    ids = [store.generate_context_id() for _ in range(3)]
    for cid in ids:
        store.append(cid, "user", "msg")
    # ids[0] is oldest. Add a 4th context — ids[0] should be evicted.
    new_id = store.generate_context_id()
    store.append(new_id, "user", "new")
    assert store.history(ids[0]) == []   # evicted → empty
    assert len(store.history(ids[1])) == 1
    assert len(store.history(ids[2])) == 1
    assert len(store.history(new_id)) == 1


def test_active_context_not_evicted():
    """An active (recently-touched) contextId is never the LRU victim."""
    store = _fresh_store(max_contexts=3)
    ids = [store.generate_context_id() for _ in range(3)]
    for cid in ids:
        store.append(cid, "user", "init")

    # Touch ids[0] again to make it MRU
    store.append(ids[0], "user", "touch")

    # Add a 4th context — should evict ids[1] (now oldest), not ids[0]
    new_id = store.generate_context_id()
    store.append(new_id, "user", "x")

    assert len(store.history(ids[0])) == 2  # still present
    assert store.history(ids[1]) == []       # evicted
    assert len(store.history(ids[2])) == 1
    assert len(store.history(new_id)) == 1


# ---------------------------------------------------------------------------
# Concurrent appends — same contextId (threads)
# ---------------------------------------------------------------------------

def test_concurrent_appends_no_loss():
    """Multiple threads appending to the same contextId lose no turns."""
    store = _fresh_store(max_turns=1000)
    ctx = store.generate_context_id()
    n = 50

    def worker(i: int) -> None:
        store.append(ctx, "user", str(i))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    turns = store.history(ctx)
    assert len(turns) == n
    values = {int(t["text"]) for t in turns}
    assert values == set(range(n))


# ---------------------------------------------------------------------------
# Per-context asyncio lock serialises overlapping async calls
# ---------------------------------------------------------------------------

def test_per_context_lock_serialises_calls():
    """Two coroutines holding the same contextId lock run sequentially."""
    store = _fresh_store()
    ctx = store.generate_context_id()
    order: List[str] = []

    async def call(label: str, delay: float) -> None:
        async with store.get_lock(ctx):
            order.append(f"{label}:start")
            await asyncio.sleep(delay)
            order.append(f"{label}:end")

    async def run() -> None:
        await asyncio.gather(call("A", 0.05), call("B", 0.0))

    asyncio.run(run())
    # One of them must complete before the other starts (serialised)
    assert order.index("A:end") < order.index("B:start") or \
           order.index("B:end") < order.index("A:start")


def test_different_contexts_run_concurrently():
    """Calls on different contextIds are NOT serialised by each other's lock."""
    store = _fresh_store()
    ctx_a = store.generate_context_id()
    ctx_b = store.generate_context_id()
    log: List[str] = []

    async def call(ctx: str, label: str, delay: float) -> None:
        async with store.get_lock(ctx):
            log.append(f"{label}:start")
            await asyncio.sleep(delay)
            log.append(f"{label}:end")

    async def run() -> None:
        await asyncio.gather(call(ctx_a, "A", 0.05), call(ctx_b, "B", 0.05))

    asyncio.run(run())
    # Both should start before either ends (true concurrency)
    assert log.index("A:start") < log.index("B:end")
    assert log.index("B:start") < log.index("A:end")


# ---------------------------------------------------------------------------
# Server integration: generated contextId surfaced in response
# ---------------------------------------------------------------------------

def test_server_generates_context_id_when_omitted(fleet_home):
    """When no contextId in request, server generates one and returns it."""
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    from fastapi.testclient import TestClient
    from a2a_fleet.server import build_app

    body = {
        "jsonrpc": "2.0",
        "id": "gen-1",
        "method": "SendMessage",
        "params": {"message": {"role": "user", "parts": [{"text": "hi"}]}},
    }
    with TestClient(build_app()) as client:
        resp = client.post("/jsonrpc", json=body)
    assert resp.status_code == 200
    msg = resp.json()["result"]["message"]
    ctx = msg.get("contextId", "")
    assert ctx and ctx != "ctx-anon"
    import uuid
    uuid.UUID(ctx)  # must be a valid UUID
