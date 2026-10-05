"""Conductor v2 B3/B6 review follow-ups: cron lineage, run-scoped event
index + tail stop, stale-firing reset, batched retention, best-effort
node_log, 8KB log rows, events query limits, schedule edge cases."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("croniter")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import engine.core.executor_shared as xs
from engine.core.dag_executor import DagRunContext
from engine.db.migrate import ensure_schema
from engine.emitter.bus import EventBus
from engine.nodes.bash import execute_bash_node
from engine.schemas.dag_node import validate_dag_node
from engine.store.run_store import STORE_LOCK, RunStore
from engine.wiring import create_engine

_YAML = """\
name: tick
description: d
nodes:
  - id: a
    bash: "echo hi"
"""


def _arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def eng():
    e = create_engine(db_path=":memory:", seed_bundled=False, write_manifest=False, crash_recovery=False)
    _arun(e.upsert_definition("tick", _YAML))
    yield e
    _arun(e.shutdown())


@pytest.fixture()
def client(eng):
    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    original = api_mod._engine
    api_mod._engine = lambda: eng
    app = FastAPI()
    app.include_router(api_mod.router)
    with TestClient(app) as c:
        yield c
    api_mod._engine = original


def _file_store(path):
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return RunStore(conn), conn


def _seed_run(conn, run_id="run-1", status="running", completed_at=None):
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT OR IGNORE INTO workflow_definitions (id, name, description, source, yaml, checksum,"
        " created_at, updated_at, kind) VALUES ('wf','wf','wf','user','x','ck',?,?,'workflow')",
        (now, now),
    )
    conn.execute(
        "INSERT INTO workflow_runs (id, workflow_id, conversation_id, working_path, user_message,"
        " status, current_phase, metadata, started_at, last_heartbeat, completed_at)"
        " VALUES (?,'wf','c','/tmp','m',?,'plan','{}',?,?,?)",
        (run_id, status, now, now, completed_at),
    )
    conn.commit()


def _wait_runs(eng, n, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = _arun(eng.list_runs())
        if len(runs) >= n:
            return runs
        time.sleep(0.05)
    raise AssertionError("no run started")


# M1 ──────────────────────────────────────────────────────────────────────────

def test_cron_fire_drops_legacy_parent_run_id(eng):
    past = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    eng._run_store.insert_scheduled_run(
        workflow_id="tick", inputs={}, trigger={"kind": "manual", "parent_run_id": "p"},
        run_at=past.isoformat(), cron_expr="*/5 * * * *",
    )
    assert _arun(eng.fire_due_scheduled_runs()) == 1
    run = _wait_runs(eng, 1)[0]
    assert run["parent_run_id"] is None
    assert "parent_run_id" not in run["metadata"]["trigger"]


# M2 ──────────────────────────────────────────────────────────────────────────

def test_run_scoped_event_paging_uses_index_without_temp_btree(eng):
    sql = ("EXPLAIN QUERY PLAN SELECT rowid AS seq, * FROM workflow_events "
           "WHERE workflow_run_id = ? AND event_type NOT IN (?) AND rowid > ? ORDER BY rowid LIMIT ?")
    with STORE_LOCK:
        plan = " ".join(r[3] for r in eng._conn.execute(sql, ("r", "node_log", 0, 10)))
    assert "idx_we_run_seq" in plan and "TEMP B-TREE" not in plan


def test_sse_tail_stops_after_terminal_event_and_restarts_on_retry(tmp_path):
    db = tmp_path / "db.sqlite"
    store, conn = _file_store(db)
    _seed_run(conn)
    other, _ = _file_store(db)
    bus = EventBus(run_store=store)

    async def go():
        gen = bus.subscribe("run-1", tail_interval_s=0.05)
        got = []

        async def collect():
            async for e in gen:
                got.append(e["event_type"])

        t = asyncio.ensure_future(collect())
        await asyncio.sleep(0.05)
        bus.emit(run_id="run-1", event_type="workflow_failed")
        await asyncio.sleep(0.05)
        other.insert_event(workflow_run_id="run-1", event_type="remote_after_end")
        await asyncio.sleep(0.4)
        missed = list(got)
        bus.emit(run_id="run-1", event_type="workflow_retried")
        other.insert_event(workflow_run_id="run-1", event_type="remote_after_retry")
        await asyncio.sleep(0.4)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        await gen.aclose()
        return missed, got

    missed, got = _arun(go())
    assert missed == ["workflow_failed"]  # tail stopped
    # tail back on: the next poll (cursor unchanged) picks up both remote rows
    assert got[1:] == ["workflow_retried", "remote_after_end", "remote_after_retry"]


# M3 ──────────────────────────────────────────────────────────────────────────

def test_at_row_bookkeeping_retried_once(eng, monkeypatch):
    past = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    row = eng._run_store.insert_scheduled_run(
        workflow_id="tick", inputs={}, trigger={"kind": "manual"}, run_at=past.isoformat(),
    )
    real, calls = eng._run_store.mark_scheduled_fired, []

    def flaky(sid):
        calls.append(sid)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        real(sid)

    monkeypatch.setattr(eng._run_store, "mark_scheduled_fired", flaky)
    assert _arun(eng.fire_due_scheduled_runs()) == 1
    assert len(calls) == 2
    with STORE_LOCK:
        status = eng._conn.execute("SELECT status FROM scheduled_runs WHERE id=?", (row["id"],)).fetchone()[0]
    assert status == "fired"


def test_claimed_at_is_per_row_claim_time(eng, monkeypatch):
    now = datetime.now(tz=timezone.utc)
    for _ in range(2):
        eng._run_store.insert_scheduled_run(
            workflow_id="tick", inputs={}, trigger={}, run_at=(now - timedelta(minutes=1)).isoformat(),
        )
    stamps = []
    real_claim = eng._run_store.claim_scheduled_run
    monkeypatch.setattr(eng._run_store, "claim_scheduled_run",
                        lambda sid, at: stamps.append(at) or real_claim(sid, at))
    real_start = eng._runner.start

    async def slow_start(*a, **kw):
        await asyncio.sleep(0.3)
        return await real_start(*a, **kw)

    monkeypatch.setattr(eng._runner, "start", slow_start)
    assert _arun(eng.fire_due_scheduled_runs(now_iso=now.isoformat())) == 2
    first, second = (datetime.fromisoformat(s) for s in stamps)
    assert first >= now and second - first >= timedelta(seconds=0.25)


# M4 ──────────────────────────────────────────────────────────────────────────

def test_retention_deletes_in_batches(tmp_path):
    store, conn = _file_store(tmp_path / "db.sqlite")
    old = int(time.time() * 1000) - 40 * 86_400_000
    for i in range(5):
        _seed_run(conn, f"old-{i}", status="completed", completed_at=old)
    _seed_run(conn, "live")
    assert store.delete_terminal_runs_older_than(30, batch=2) == 5
    assert [r[0] for r in conn.execute("SELECT id FROM workflow_runs")] == ["live"]


# M5 ──────────────────────────────────────────────────────────────────────────

def test_node_log_dropped_not_blocking_when_store_busy(tmp_path):
    store, conn = _file_store(tmp_path / "db.sqlite")
    _seed_run(conn)
    bus = EventBus(run_store=store)
    held, release = threading.Event(), threading.Event()

    def hog():
        with STORE_LOCK:
            held.set()
            release.wait(5)

    th = threading.Thread(target=hog)
    th.start()
    held.wait(2)
    try:
        t0 = time.monotonic()
        bus.emit(run_id="run-1", event_type="node_log", data={"text": "x"})
        elapsed = time.monotonic() - t0
    finally:
        release.set()
        th.join()
    assert elapsed < 0.5
    assert store.list_recent_events("run-1", types=["node_log"]) == []
    bus.emit(run_id="run-1", event_type="node_log", data={"text": "y"})  # free again: persisted
    assert [e["data"]["text"] for e in store.list_recent_events("run-1", types=["node_log"])] == ["y"]


# M6 ──────────────────────────────────────────────────────────────────────────

def test_node_log_rows_at_most_8kb_and_multibyte_safe():
    events = []
    from unittest.mock import AsyncMock
    ctx = DagRunContext(
        run_id="r", emit_event=lambda t, p: events.append((t, dict(p))),
        get_run_status=AsyncMock(return_value="running"), pause_run=AsyncMock(),
        cancel_run=AsyncMock(), send_message=AsyncMock(), get_subgraph_yaml=lambda ref: None,
    )
    node, errors = validate_dag_node(
        {"id": "big", "bash": "python3 -c \"print('é' * 60000, end='')\"", "timeout": 20000}, 0,
    )
    assert not errors
    res = _arun(execute_bash_node(node, {}, ctx))
    assert res.state == "completed"
    logs = [p["text"] for t, p in events if t == "node_log"]
    assert len(logs) > 1 and all(len(t.encode()) <= xs.NODE_LOG_FLUSH_BYTES for t in logs)
    assert "".join(logs) == "é" * 60000


def test_query_truncates_node_log_by_bytes(eng, client):
    run = _arun(eng.start_run("tick", {}, {"kind": "manual", "conversation_id": "c"}))
    eng._run_store.insert_event(workflow_run_id=run["id"], event_type="node_log", data={"text": "é" * 5000})
    body = client.get(f"/runs/{run['id']}/events?type=node_log").json()
    text = body["events"][-1]["data"]["text"]
    assert len(text.encode()) <= 8192 and body["events"][-1]["data"]["text_truncated"] is True
    assert text == "é" * 4096


# L1 / L2 ─────────────────────────────────────────────────────────────────────

def test_events_rowid_order_cursor_max_and_type_limits(eng, client):
    run = _arun(eng.start_run("tick", {}, {"kind": "manual", "conversation_id": "c"}))
    rid = run["id"]
    s1 = eng._run_store.insert_event(workflow_run_id=rid, event_type="zz_a")[1]
    s2 = eng._run_store.insert_event(workflow_run_id=rid, event_type="zz_b")[1]
    with STORE_LOCK:  # skewed clock: the later row looks older
        eng._conn.execute("UPDATE workflow_events SET created_at = 1 WHERE rowid = ?", (s2,))
        eng._conn.commit()
    body = client.get(f"/runs/{rid}/events?type=zz_a,zz_b").json()
    assert [e["seq"] for e in body["events"]] == [s1, s2] and body["cursor"] == s2

    assert client.get(f"/runs/{rid}/events?type=" + ",".join(f"t{i}" for i in range(33))).status_code == 400
    assert client.get(f"/runs/{rid}/events?type=Bad-Name").status_code == 400


# L4 / L5 / L7 ────────────────────────────────────────────────────────────────

def test_schedule_edge_cases(eng, client, monkeypatch):
    store = eng._run_store
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()
    at = store.insert_scheduled_run(workflow_id="tick", inputs={}, trigger={}, run_at=future)["id"]
    store.set_schedule_status(at, "firing")
    assert client.patch(f"/schedules/{at}", json={"enabled": True}).status_code == 409  # L4

    store.set_schedule_status(at, "cancelled")
    store.mark_scheduled_fired(at)  # L5: only a firing row becomes fired
    assert store.get_scheduled_run(at)["status"] == "cancelled"

    cron_id = store.insert_scheduled_run(workflow_id="tick", inputs={}, trigger={}, run_at=future,
                                         cron_expr="*/5 * * * *")["id"]
    store.set_schedule_status(cron_id, "disabled")
    import engine.cron.schedule as cron

    def no_croniter(*a):
        raise ImportError("croniter")

    monkeypatch.setattr(cron, "next_fire", no_croniter)
    assert client.patch(f"/schedules/{cron_id}", json={"enabled": True}).status_code == 501  # L7
    monkeypatch.setattr(cron, "next_fire", lambda *a: (_ for _ in ()).throw(ValueError("never fires")))
    r = client.patch(f"/schedules/{cron_id}", json={"enabled": True})
    assert r.status_code == 400 and "never fires" in r.json()["error"]
    assert store.get_scheduled_run(cron_id)["status"] == "disabled"
