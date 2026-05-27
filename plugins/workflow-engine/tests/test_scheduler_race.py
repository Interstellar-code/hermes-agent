"""Scheduler claim race — at most one tick can claim a given pending row."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta

from engine.db.client import open_db
from engine.db.migrate import ensure_schema
from engine.store.run_store import RunStore


def _setup_store():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return RunStore(conn), conn


def test_two_concurrent_claims_only_one_wins():
    store, _ = _setup_store()
    past = (datetime.now(tz=timezone.utc) - timedelta(seconds=60)).isoformat()
    row = store.insert_scheduled_run(
        workflow_id="w1",
        inputs={},
        trigger={"kind": "test"},
        run_at=past,
    )
    sid = row["id"]
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    a = store.claim_scheduled_run(sid, now_iso)
    b = store.claim_scheduled_run(sid, now_iso)

    assert (a, b) == (True, False), f"exactly one claim must win, got {(a, b)}"


def test_claim_skips_future_row():
    store, _ = _setup_store()
    future = (datetime.now(tz=timezone.utc) + timedelta(seconds=120)).isoformat()
    row = store.insert_scheduled_run(
        workflow_id="w1",
        inputs={},
        trigger={},
        run_at=future,
    )
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    assert store.claim_scheduled_run(row["id"], now_iso) is False


def test_list_due_orders_by_priority_then_run_at():
    store, _ = _setup_store()
    base = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
    a = store.insert_scheduled_run(
        workflow_id="w1", inputs={}, trigger={},
        run_at=(base + timedelta(seconds=1)).isoformat(), priority=0,
    )
    b = store.insert_scheduled_run(
        workflow_id="w1", inputs={}, trigger={},
        run_at=(base + timedelta(seconds=2)).isoformat(), priority=10,
    )
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    due = store.list_due_scheduled_runs(now_iso)
    assert [d["id"] for d in due[:2]] == [b["id"], a["id"]]
