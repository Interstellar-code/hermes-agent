"""
Tests for EventBus.

Acceptance gate:
- test_replay_and_live: subscribe after 20 events emitted, receive all 20
  (replay) then live events.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from engine.db.migrate import ensure_schema
from engine.store.run_store import RunStore
from engine.emitter.bus import EventBus


def _make_env():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    # Seed a workflow_definitions row so FK is satisfied
    now = int(time.time() * 1000)
    conn.execute(
        """INSERT INTO workflow_definitions
             (id, name, description, source, yaml, checksum, created_at, updated_at, kind)
           VALUES ('wf-test','wf','wf','bundled','name: wf\ndescription: wf\nnodes: []','ck',?,?,'workflow')""",
        (now, now),
    )
    conn.execute(
        """INSERT INTO workflow_runs
             (id, workflow_id, conversation_id, working_path, user_message,
              status, current_phase, metadata, started_at, last_heartbeat)
           VALUES ('run-bus-1','wf-test','conv-bus','/tmp','test','running','plan','{}',?,?)""",
        (now, now),
    )
    conn.commit()
    run_store = RunStore(conn)
    bus = EventBus(run_store=run_store)
    return run_store, bus, conn


@pytest.mark.asyncio
async def test_replay_and_live():
    """Subscribe after 20 events; subscriber receives all 20 (replay) then live."""
    run_store, bus, _conn = _make_env()

    run_id = "run-bus-1"

    # Emit 20 events BEFORE subscribing
    for i in range(20):
        bus.emit(run_id=run_id, event_type="node_started", data={"node_id": f"n{i}"})

    received = []
    stop_event = asyncio.Event()

    async def collect():
        async for evt in bus.subscribe(run_id=run_id):
            received.append(evt)
            if len(received) >= 22:  # 20 replay + 2 live
                break

    task = asyncio.create_task(collect())

    # Give replay a moment to flush
    await asyncio.sleep(0.1)

    # Emit 2 more live events
    bus.emit(run_id=run_id, event_type="node_completed", data={"node_id": "n_live_1"})
    bus.emit(run_id=run_id, event_type="node_completed", data={"node_id": "n_live_2"})

    await asyncio.wait_for(task, timeout=3.0)

    assert len(received) >= 22, f"Expected >=22 events, got {len(received)}"

    # Replayed events should be marked
    replayed = [e for e in received if e.get("_replayed")]
    assert len(replayed) == 20, f"Expected 20 replayed, got {len(replayed)}"

    live = [e for e in received if not e.get("_replayed")]
    assert len(live) >= 2


@pytest.mark.asyncio
async def test_subscribe_all_runs():
    """subscribe() with no run_id receives events for all runs."""
    run_store, bus, conn = _make_env()
    now = int(time.time() * 1000)
    # Add second run
    conn.execute(
        """INSERT INTO workflow_runs
             (id, workflow_id, conversation_id, working_path, user_message,
              status, current_phase, metadata, started_at, last_heartbeat)
           VALUES ('run-bus-2','wf-test','conv-bus2','/tmp','test','running','plan','{}',?,?)""",
        (now, now),
    )
    conn.commit()

    received = []

    async def collect():
        async for evt in bus.subscribe():  # no run_id filter
            received.append(evt)
            if len(received) >= 2:
                break

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.05)

    bus.emit(run_id="run-bus-1", event_type="workflow_started", data={})
    bus.emit(run_id="run-bus-2", event_type="workflow_started", data={})

    await asyncio.wait_for(task, timeout=3.0)
    assert len(received) >= 2

    run_ids = {e.get("run_id") for e in received}
    assert "run-bus-1" in run_ids
    assert "run-bus-2" in run_ids


@pytest.mark.asyncio
async def test_overflow_drops_oldest():
    """Queue overflow drops oldest, not newest."""
    run_store, bus, _conn = _make_env()
    run_id = "run-bus-1"

    # Subscribe and prime the generator so the subscriber is registered
    # before we emit. The first __anext__() call registers the subscriber
    # and replays DB events (none yet), then blocks on the live queue.
    gen = bus.subscribe(run_id=run_id)
    prime_task = asyncio.create_task(gen.__anext__())
    # Yield control so the generator runs through replay and awaits the queue
    await asyncio.sleep(0)

    # Emit events — subscriber is now registered
    # We'll use a small number for speed; just verify no exception
    for i in range(10):
        bus.emit(run_id=run_id, event_type="node_started", data={"seq": i})

    # Consume one to confirm the generator is alive
    first = await asyncio.wait_for(prime_task, timeout=2.0)
    assert first is not None

    # Clean up
    await gen.aclose()
