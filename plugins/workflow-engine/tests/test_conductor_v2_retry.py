"""Conductor v2 B4: retry a failed, cancelled or crashed run in place."""
from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.store.run_store import STORE_LOCK
from engine.wiring import create_engine


def _arun(coro):
    # Not asyncio.run: it clears the thread's current loop (breaks later tests).
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def eng():
    e = create_engine(db_path=":memory:", seed_bundled=False, write_manifest=False, crash_recovery=False)
    yield e
    _arun(e.shutdown())


@pytest.fixture()
def abc(eng, tmp_path):
    """A → B → C; B fails until ``flag`` exists; A appends to ``count``."""
    flag, count = tmp_path / "flag", tmp_path / "count"
    yaml_text = f"""\
name: abc
description: d
nodes:
  - id: A
    bash: "echo a >> {count}; echo a"
  - id: B
    bash: "test -f {flag} || {{ echo nope >&2; exit 1; }}; echo b"
    depends_on: [A]
    retry: {{max_attempts: 1, delay_ms: 1000, on_error: all}}
  - id: C
    bash: echo c
    depends_on: [B]
"""
    _arun(eng.upsert_definition("abc", yaml_text))
    return flag, count


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


def _failed_run(eng):
    run = _arun(eng.start_run("abc", {}, {"kind": "manual", "conversation_id": "c"}))
    _wait(eng, run["id"], {"failed"})
    return run["id"]


def _rows(eng, run_id):
    return {r["dag_node_id"]: r for r in _arun(eng.list_node_runs(run_id))}


def _events(eng, run_id, etype):
    return [e for e in eng._run_store.list_events(run_id, limit=500) if e["event_type"] == etype]


def test_retry_resumes_from_failed_node(eng, abc, client):
    flag, count = abc
    run_id = _failed_run(eng)
    before = _rows(eng, run_id)
    assert (before["B"]["status"], before["B"]["retries"]) == ("failed", 1)
    assert "C" not in before or before["C"]["status"] == "skipped"  # never ran

    flag.touch()
    r = client.post(f"/runs/{run_id}/retry", json={"actor": " switchui "})
    assert r.status_code == 200, r.text
    assert r.json()["run"]["status"] == "running"
    run = _wait(eng, run_id, {"completed", "failed"})
    assert run["status"] == "completed" and run["error"] is None

    rows = _rows(eng, run_id)
    assert len(_arun(eng.list_node_runs(run_id))) == 3
    assert {k: v["status"] for k, v in rows.items()} == {"A": "completed", "B": "completed", "C": "completed"}
    assert rows["B"]["retries"] == 0 and rows["B"]["error"] is None
    assert count.read_text().count("a") == 1  # A kept, not re-run

    retried = _events(eng, run_id, "workflow_retried")
    assert [e["data"] for e in retried] == [{"retry_from": None, "actor": "switchui", "previous_status": "failed"}]
    skipped = [e["data"]["node_id"] for e in _events(eng, run_id, "node_skipped")
               if e["data"].get("reason") == "prior_success"]
    assert skipped == ["A"]
    assert _events(eng, run_id, "workflow_resumed_execute")[-1]["data"]["rerun_nodes"] == ["B", "C"]


def test_retry_from_node_reruns_it_and_descendants(eng, abc, client):
    flag, count = abc
    run_id = _failed_run(eng)
    flag.touch()
    assert client.post(f"/runs/{run_id}/retry", json={"from_node_id": "A"}).status_code == 200
    assert _wait(eng, run_id, {"completed", "failed"})["status"] == "completed"
    assert count.read_text().count("a") == 2
    assert len(_rows(eng, run_id)) == 3


def test_retry_rejections(eng, abc, client):
    run_id = _failed_run(eng)
    assert client.post("/runs/nope/retry", json={}).status_code == 404
    r = client.post(f"/runs/{run_id}/retry", json={"from_node_id": "Z"})
    assert r.status_code == 400 and "not a node" in r.json()["error"]
    assert client.post(f"/runs/{run_id}/retry", json={"from_node_id": 3}).status_code == 400
    assert client.post(f"/runs/{run_id}/retry", json={"actor": "a\nb"}).status_code == 400
    assert client.post(f"/runs/{run_id}/retry", content=b"[]",
                       headers={"content-type": "application/json"}).status_code == 400
    # 400s left the run untouched
    assert _arun(eng.get_run(run_id))["status"] == "failed"

    # A live run (fresh heartbeat) is still owned by a process: 409.
    live = eng._run_store.create_workflow_run(
        workflow_id="abc", conversation_id="c", working_path="/tmp", user_message="m",
    )
    eng._run_store.update_workflow_run(live["id"], status="running")
    r = client.post(f"/runs/{live['id']}/retry", json={})
    assert r.status_code == 409 and "live process" in r.json()["error"]

    eng._run_store.update_workflow_run(live["id"], status="completed")
    r = client.post(f"/runs/{live['id']}/retry", json={})
    assert r.status_code == 409 and "completed" in r.json()["error"]


def test_crashed_run_retry_reexecutes_running_node(eng, abc, client):
    flag, _ = abc
    run_id = _failed_run(eng)
    # Simulate an owner that died mid-B: run still 'running' with a heartbeat
    # older than STALE_MS (300s), B left 'running', C never started.
    stale = int(time.time() * 1000) - 400_000
    with STORE_LOCK:
        eng._conn.execute(
            "UPDATE workflow_runs SET status='running', error=NULL, completed_at=NULL, "
            "last_heartbeat=? WHERE id=?", (stale, run_id))
        eng._conn.execute(
            "UPDATE node_runs SET status='running', error=NULL WHERE workflow_run_id=? AND dag_node_id='B'",
            (run_id,))
        eng._conn.execute("DELETE FROM node_runs WHERE workflow_run_id=? AND dag_node_id='C'", (run_id,))
        eng._conn.commit()

    flag.touch()
    r = client.post(f"/runs/{run_id}/retry", json={})
    assert r.status_code == 200, r.text
    assert _wait(eng, run_id, {"completed", "failed"})["status"] == "completed"
    rows = _rows(eng, run_id)
    assert {k: v["status"] for k, v in rows.items()} == {"A": "completed", "B": "completed", "C": "completed"}
    assert _events(eng, run_id, "workflow_retried")[0]["data"]["previous_status"] == "running"


def test_reopen_marks_orphaned_rows_crashed(eng, abc):
    run_id = _failed_run(eng)
    store = eng._run_store
    b = _rows(eng, run_id)["B"]
    store.update_node_run(b["id"], {"status": "paused"})
    assert store.reopen_run(run_id) is True
    got = store.get_node_run(b["id"])
    assert (got["status"], got["error"]) == ("failed", "crashed: owner process stopped")
    assert store.reopen_run(run_id) is False  # now running with a fresh heartbeat


def test_cancelled_run_retry_completes(eng, tmp_path, client):
    yaml_text = """\
name: slow
description: d
nodes:
  - id: s
    bash: "sleep 0.5; echo s"
  - id: t
    bash: echo t
    depends_on: [s]
"""
    _arun(eng.upsert_definition("slow", yaml_text))
    run = _arun(eng.start_run("slow", {}, {"kind": "manual", "conversation_id": "c"}))
    time.sleep(0.2)
    _arun(eng.cancel_run(run["id"]))
    _wait(eng, run["id"], {"cancelled"})
    assert client.post(f"/runs/{run['id']}/retry", json={}).status_code == 200
    assert _wait(eng, run["id"], {"completed", "failed"})["status"] == "completed"


def test_health_lists_retry_run(client):
    body = client.get("/health").json()
    assert "retry_run" in body["features"] and body["version"] == "0.4.0"
