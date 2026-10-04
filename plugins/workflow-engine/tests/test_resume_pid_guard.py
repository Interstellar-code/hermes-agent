"""Crash recovery by per-run heartbeat (#49, L2-02).

mark_crashed_runs fails only runs whose owner stopped heartbeating; runs live
in this or another process (fresh heartbeat) are left alone.
"""
from __future__ import annotations

import sqlite3
import time

from engine.db.migrate import ensure_schema
from engine.store.run_store import RunStore


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = int(time.time() * 1000)
    conn.execute(
        """INSERT INTO workflow_definitions
             (id, name, description, source, yaml, checksum, created_at, updated_at, kind)
           VALUES ('wf', 'wf', 'wf', 'bundled', 'nodes: []', 'test', ?, ?, 'workflow')""",
        (now, now),
    )
    conn.commit()
    return conn


def _running_run(run_store: RunStore) -> str:
    run = run_store.create_workflow_run(
        workflow_id="wf",
        conversation_id="c1",
        working_path="/tmp",
        user_message="go",
    )
    run_store.update_workflow_run(run["id"], status="running")
    return run["id"]


def _age(store: RunStore, run_id: str, ms: int) -> None:
    store._conn.execute(
        "UPDATE workflow_runs SET last_heartbeat = last_heartbeat - ? WHERE id = ?", (ms, run_id)
    )
    store._conn.commit()


def test_live_run_owned_elsewhere_left_running():
    """A fresh heartbeat (another process's live run) is never reaped."""
    store = RunStore(_make_conn())
    run_id = _running_run(store)
    assert store.mark_crashed_runs() == 0
    assert store.get_workflow_run(run_id)["status"] == "running"


def test_stale_heartbeat_marks_crashed():
    store = RunStore(_make_conn())
    run_id = _running_run(store)
    _age(store, run_id, 10 * 60 * 1000)
    assert store.mark_crashed_runs() == 1
    assert store.get_workflow_run(run_id)["status"] == "failed"


def test_heartbeat_keeps_run_alive():
    store = RunStore(_make_conn())
    run_id = _running_run(store)
    _age(store, run_id, 10 * 60 * 1000)
    store.heartbeat_runs([run_id])
    assert store.mark_crashed_runs() == 0


def test_retention_sweep_drops_old_terminal_runs_only():
    store = RunStore(_make_conn())
    old = _running_run(store)
    live = _running_run(store)
    store.update_workflow_run(old, status="completed")
    store._conn.execute(
        "UPDATE workflow_runs SET completed_at = completed_at - ? WHERE id = ?",
        (31 * 86_400_000, old),
    )
    assert store.delete_terminal_runs_older_than(30) == 1
    assert store.get_workflow_run(old) is None
    assert store.get_workflow_run(live) is not None
