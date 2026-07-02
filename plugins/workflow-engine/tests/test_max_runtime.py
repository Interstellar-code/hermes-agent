"""max_runtime_s enforcement — a run that exceeds the cap finalises as failed."""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from engine.db.migrate import ensure_schema
from engine.store.run_store import RunStore
from engine.store.definition_store import DefinitionStore
from engine.emitter.bus import EventBus
from engine.runtime.runner import WorkflowRunner


SLEEP_YAML = """
name: sleep-workflow
description: Sleeps past max_runtime_s
nodes:
  - id: sleeper
    bash: sleep 30
"""


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _seed(def_store: DefinitionStore, wf_id: str, yaml_text: str) -> None:
    now = int(time.time() * 1000)
    def_store._conn.execute(
        """INSERT INTO workflow_definitions
             (id, name, description, source, yaml, checksum, created_at, updated_at, kind)
           VALUES (?, ?, ?, 'bundled', ?, 'test', ?, ?, 'workflow')""",
        (wf_id, wf_id, wf_id, yaml_text, now, now),
    )
    def_store._conn.commit()


@pytest.mark.asyncio
async def test_run_finalises_failed_on_max_runtime_exceeded():
    conn = _make_conn()
    run_store = RunStore(conn)
    def_store = DefinitionStore(conn)
    bus = EventBus(run_store=run_store)
    runner = WorkflowRunner(run_store, def_store, bus)

    _seed(def_store, "sleeper", SLEEP_YAML)

    run = await runner.start(
        "sleeper", {}, {"kind": "manual"},
        max_runtime_s=1,
    )
    run_id = run["id"]

    # Wait for the run to settle, but give wait_for some headroom.
    await runner.wait_for(run_id, timeout=10.0)

    final = run_store.get_workflow_run(run_id)
    assert final is not None
    assert final["status"] == "failed", final
    assert final.get("error") == "max_runtime_exceeded"

    # workflow_failed event with the matching reason should be present.
    events = run_store.list_recent_events(run_id, limit=50)
    failed = [
        e for e in events
        if e["event_type"] == "workflow_failed"
    ]
    assert failed, "expected a workflow_failed event"
    data = failed[-1].get("data") or {}
    assert data.get("reason") == "max_runtime_exceeded"
