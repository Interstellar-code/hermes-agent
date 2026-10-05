"""Conductor v2 deploy-blocker review fixes: rollback on sqlite errors,
retry ownership (heartbeat release + retry_epoch), single approval_requested,
non-blocking retention sweep, stale-firing re-check, retry resets decisions."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import engine.runtime.runner as runner_mod
from engine.db.migrate import ensure_schema
from engine.emitter.bus import EventBus
from engine.store.run_store import STORE_LOCK, RunStore
from engine.wiring import create_engine


def _arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def eng():
    e = create_engine(db_path=":memory:", seed_bundled=False, write_manifest=False, crash_recovery=False)
    _arun(e.upsert_definition("wf", "name: wf\ndescription: d\nnodes:\n  - id: a\n    bash: echo a\n"))
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


def _wait(eng, run_id, statuses, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = _arun(eng.get_run(run_id))
        if r["status"] in statuses:
            return r
        time.sleep(0.05)
    raise AssertionError(f"run stuck in {r['status']}")


def _events(eng, run_id, etype):
    return [e for e in eng._run_store.list_events(run_id, limit=500) if e["event_type"] == etype]


def _sql(eng, sql, args=()):
    with STORE_LOCK:
        eng._conn.execute(sql, args)
        eng._conn.commit()


def _live_run(eng, status="running"):
    run = eng._run_store.create_workflow_run(
        workflow_id="wf", conversation_id="c", working_path="/tmp", user_message="m",
    )
    eng._run_store.update_workflow_run(run["id"], status=status)  # fresh heartbeat
    return run["id"]


# HIGH 1 ─────────────────────────────────────────────────────────────────────

def test_dropped_best_effort_event_does_not_poison_connection(tmp_path):
    db = str(tmp_path / "db.sqlite")
    a = sqlite3.connect(db, check_same_thread=False)
    a.row_factory = sqlite3.Row
    for q in ("PRAGMA journal_mode=WAL", "PRAGMA foreign_keys=ON", "PRAGMA busy_timeout=5000"):
        a.execute(q)
    ensure_schema(a, lock_path=tmp_path / "lock")
    store = RunStore(a)
    bus = EventBus(run_store=store)
    a.execute("PRAGMA foreign_keys=OFF")
    a.execute("INSERT INTO workflow_runs (id, workflow_id, conversation_id, working_path, user_message,"
              " status, started_at, last_heartbeat) VALUES ('r','w','c','/','m','running',0,0)")
    a.commit()
    a.execute("PRAGMA foreign_keys=ON")

    b = sqlite3.connect(db, isolation_level=None)
    b.execute("BEGIN IMMEDIATE")  # another process mid-write
    bus.emit(run_id="r", event_type="node_log", data={"text": "x"})  # dropped
    assert not a.in_transaction
    b.execute("INSERT INTO schema_meta (key, value) VALUES ('p', '1')")
    b.execute("COMMIT")

    store.heartbeat_runs(["r"])
    bus.emit(run_id="r", event_type="workflow_completed")
    assert store.finish_workflow_run_if_running("r", status="completed")
    rows = b.execute("SELECT event_type FROM workflow_events").fetchall()
    assert rows == [("workflow_completed",)]


# HIGH 2 ─────────────────────────────────────────────────────────────────────

def test_heartbeat_refreshes_owned_run_whatever_its_status(eng):
    rid = _live_run(eng)
    eng._run_store.cancel_workflow_run(rid)  # cross-process cancel
    _sql(eng, "UPDATE workflow_runs SET last_heartbeat = 1 WHERE id = ?", (rid,))
    eng._run_store.heartbeat_runs([rid])
    assert eng._run_store.get_workflow_run(rid)["last_heartbeat"] > "2000"


def test_cancelled_or_failed_run_with_live_owner_is_not_retryable(eng, client):
    # Owner in another process: still heartbeating after the status flip.
    for status in ("cancelled", "failed"):
        rid = _live_run(eng)
        _sql(eng, "UPDATE workflow_runs SET status = ? WHERE id = ?", (status, rid))
        r = client.post(f"/runs/{rid}/retry", json={})
        assert r.status_code == 409 and "live process" in r.json()["error"]
        assert eng._run_store.reopen_run(rid) is False
        # Owner's task ended: heartbeat released -> retryable, epoch bumped.
        eng._run_store.release_run(rid)
        assert eng._run_store.get_workflow_run(rid)["last_heartbeat"] is None
        assert eng._run_store.reopen_run(rid) is True
        assert eng._run_store.get_workflow_run(rid)["retry_epoch"] == 1


def test_crash_threshold_is_stale_ms(eng):
    rid = _live_run(eng)
    _sql(eng, "UPDATE workflow_runs SET last_heartbeat = ? WHERE id = ?",
         (int(time.time() * 1000) - 120_000, rid))
    assert eng._run_store.reopen_run(rid) is False  # 2 min: maybe just slow
    _sql(eng, "UPDATE workflow_runs SET last_heartbeat = ? WHERE id = ?",
         (int(time.time() * 1000) - 301_000, rid))
    assert eng._run_store.reopen_run(rid) is True


def test_superseded_owner_stops_and_never_finalises(eng):
    _arun(eng.upsert_definition("slow", """\
name: slow
description: d
nodes:
  - id: s
    bash: "sleep 0.6; echo s"
  - id: t
    bash: echo t
    depends_on: [s]
"""))
    run = _arun(eng.start_run("slow", {}, {"kind": "manual", "conversation_id": "c"}))
    rid = run["id"]
    time.sleep(0.2)
    # Another process retried the run meanwhile (this owner looked dead).
    _sql(eng, "UPDATE workflow_runs SET retry_epoch = retry_epoch + 1 WHERE id = ?", (rid,))
    deadline = time.monotonic() + 10
    while eng._runner._tasks.get(rid) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    got = eng._run_store.get_workflow_run(rid)
    assert got["status"] == "running"  # the new owner's attempt, untouched
    assert got["last_heartbeat"] is not None  # not released by the old owner
    nodes = {n["dag_node_id"] for n in eng._run_store.list_node_runs(rid)}
    assert "t" not in nodes  # stopped at the layer boundary
    assert not _events(eng, rid, "workflow_completed")
    assert not _events(eng, rid, "platform_message")


def test_lost_double_retry_race_is_409_already_retried(eng, client, monkeypatch):
    rid = _live_run(eng)
    _sql(eng, "UPDATE workflow_runs SET status = 'failed', last_heartbeat = 0 WHERE id = ?", (rid,))
    store = eng._run_store
    real = store.reopen_run

    def other_process_wins(run_id, **kw):
        assert real(run_id)  # a concurrent retry got there first
        return real(run_id, **kw)

    monkeypatch.setattr(store, "reopen_run", other_process_wins)
    r = client.post(f"/runs/{rid}/retry", json={})
    assert r.status_code == 409 and r.json()["error"] == "run already retried"


# approval_requested ──────────────────────────────────────────────────────────

def test_approval_requested_once_and_retry_resets_decision(eng, client):
    _arun(eng.upsert_definition("gated", """\
name: gated
description: d
nodes:
  - id: gate
    approval:
      message: ok?
  - id: after
    bash: echo after
    depends_on: [gate]
"""))
    rid = _arun(eng.start_run("gated", {}, {"kind": "manual", "conversation_id": "c"}))["id"]
    _wait(eng, rid, {"paused"})
    assert len(_events(eng, rid, "approval_requested")) == 1

    _arun(eng.approve(rid, "gate", "reject", comment="no", actor="me"))
    _wait(eng, rid, {"failed"})
    gate = eng._run_store.find_node_run(rid, "gate")
    assert gate["approval_response"] == "no" and gate["metadata"]["approved_by"] == "me"

    # reject path released the run: retryable at once
    assert client.post(f"/runs/{rid}/retry", json={}).status_code == 200
    _wait(eng, rid, {"paused"})
    gate = eng._run_store.find_node_run(rid, "gate")
    assert gate["status"] == "paused" and gate["approval_response"] is None
    assert "approved_by" not in (gate["metadata"] or {})
    assert len(_events(eng, rid, "approval_requested")) == 2  # one per attempt


# MEDIUM / LOW ───────────────────────────────────────────────────────────────

def test_retention_sweep_does_not_block_heartbeats(eng, monkeypatch):
    monkeypatch.setattr(runner_mod, "RETENTION_SWEEP_S", 0.0)
    runner, store = eng._runner, eng._run_store
    release, beats = threading.Event(), []
    monkeypatch.setattr(store, "delete_terminal_runs_older_than", lambda days: release.wait(5) and 0)
    monkeypatch.setattr(store, "heartbeat_runs", lambda ids: beats.append(1))
    runner.retention_days = 30

    async def go():
        t = asyncio.ensure_future(runner.heartbeat_forever(interval_s=0.02))
        await asyncio.sleep(0.3)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)

    try:
        _arun(go())
    finally:
        release.set()
    assert len(beats) > 3


def test_reset_stale_firing_rechecks_cutoff(eng):
    store = eng._run_store
    past = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    row = store.insert_scheduled_run(
        workflow_id="wf", inputs={}, trigger={}, run_at=past.isoformat(), cron_expr="*/5 * * * *",
    )
    assert store.claim_scheduled_run(row["id"], (past + timedelta(seconds=1)).isoformat())
    now = datetime.now(tz=timezone.utc)

    def reclaimed_meanwhile(expr):
        # another ticker reset + re-claimed it between the SELECT and UPDATE
        _sql(eng, "UPDATE scheduled_runs SET trigger_json = json_set(trigger_json, "
                  "'$.claimed_at', ?) WHERE id = ?", (now.isoformat(), row["id"]))
        return (now + timedelta(minutes=5)).isoformat()

    store.reset_stale_firing((now - timedelta(seconds=20)).isoformat(), reclaimed_meanwhile)
    assert store.get_scheduled_run(row["id"])["status"] == "firing"
