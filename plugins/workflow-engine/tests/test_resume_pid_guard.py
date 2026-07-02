"""Resume policy PID guard — regression test for #49.

mark_crashed_runs must distinguish a genuine process restart (PID differs from
the one persisted in schema_meta) from an in-process plugin re-initialization
(same PID), where live asyncio run tasks are still finalising themselves.
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


def test_first_boot_marks_running_as_crashed():
    """No prior PID recorded → treat in-flight rows as a real crash."""
    store = RunStore(_make_conn())
    run_id = _running_run(store)
    assert store.mark_crashed_runs(boot_pid=4242) == 1
    assert store.get_workflow_run(run_id)["status"] == "failed"


def test_same_pid_reinit_leaves_runs_running():
    """Second boot with the same PID = in-process reinit → do not crash."""
    store = RunStore(_make_conn())
    store.mark_crashed_runs(boot_pid=4242)  # records boot_pid
    run_id = _running_run(store)
    assert store.mark_crashed_runs(boot_pid=4242) == 0
    assert store.get_workflow_run(run_id)["status"] == "running"


def test_different_pid_marks_crashed():
    """Boot under a new PID = real process restart → crash in-flight rows."""
    store = RunStore(_make_conn())
    store.mark_crashed_runs(boot_pid=4242)
    run_id = _running_run(store)
    assert store.mark_crashed_runs(boot_pid=9999) == 1
    assert store.get_workflow_run(run_id)["status"] == "failed"


def test_legacy_no_pid_marks_crashed():
    """boot_pid=None preserves the unconditional legacy behaviour."""
    store = RunStore(_make_conn())
    run_id = _running_run(store)
    assert store.mark_crashed_runs() == 1
    assert store.get_workflow_run(run_id)["status"] == "failed"
