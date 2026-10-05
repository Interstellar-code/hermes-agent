"""Conductor v2 B2: recorded retry attempts, approver, event step fields."""
from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

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


async def _wait(eng, run_id, statuses, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await eng.get_run(run_id)
        if r["status"] in statuses:
            return r
        await asyncio.sleep(0.05)
    raise AssertionError(f"run stuck in {r['status']}")


def test_retries_recorded_on_one_row(eng, tmp_path):
    counter = tmp_path / "n"
    # Two nodes in one layer (run concurrently): `always` fails all 3
    # attempts, `flaky` fails twice then succeeds.
    yaml_text = f"""\
name: retry-me
description: d
nodes:
  - id: always
    bash: "echo boom >&2; exit 1"
    retry: {{max_attempts: 2, delay_ms: 1000, on_error: all}}
  - id: flaky
    bash: "n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}; [ $n -ge 3 ] || exit 1; echo ok"
    retry: {{max_attempts: 2, delay_ms: 1000, on_error: all}}
"""

    async def go():
        await eng.upsert_definition("retry-me", yaml_text)
        run = await eng.start_run("retry-me", {}, {"kind": "manual", "conversation_id": "c"})
        await _wait(eng, run["id"], {"failed"})
        rows = await eng.list_node_runs(run["id"])
        events = eng._run_store.list_events(run["id"], limit=500)
        return rows, events

    rows, events = _arun(go())
    by_node = {}
    for r in rows:
        by_node.setdefault(r["dag_node_id"], []).append(r)
    assert len(by_node["always"]) == 1 and len(by_node["flaky"]) == 1

    always, flaky = by_node["always"][0], by_node["flaky"][0]
    assert (always["status"], always["retries"], always["max_retries"]) == ("failed", 2, 2)
    assert always["retry_delay_ms"] == 2000  # backoff of the last wait
    # Reused row was cleared on each re-start: the success carries no stale error.
    assert (flaky["status"], flaky["retries"], flaky["error"], flaky["summary"]) == ("completed", 2, None, "ok")

    retrying = [e for e in events if e["event_type"] == "node_retrying" and e["data"]["node_id"] == "always"]
    assert [(e["data"]["attempt"], e["data"]["max_attempts"]) for e in retrying] == [(2, 3), (3, 3)]
    assert all(e["node_run_id"] == always["id"] and "boom" in e["data"]["error"] for e in retrying)

    node_events = [e for e in events if (e["data"] or {}).get("node_id") == "flaky"]
    assert node_events and all(e["step_name"] == "flaky" and e["step_index"] == 1 for e in node_events)
    run_events = [e for e in events if e["event_type"] == "workflow_started"]
    assert run_events[0]["step_name"] is None and run_events[0]["step_index"] is None


APPROVAL_YAML = """\
name: needs-approval
description: d
nodes:
  - id: gate
    approval:
      message: ok?
"""


@pytest.fixture()
def client(eng):
    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    original = api_mod._engine
    api_mod._engine = lambda: eng
    app = FastAPI()
    app.include_router(api_mod.router)
    with TestClient(app) as c:
        c.post("/definitions", json={"id": "needs-approval", "name": "n", "yaml": APPROVAL_YAML, "source": "user"})
        yield c
    api_mod._engine = original


def _paused_gate(client):
    run_id = client.post("/runs", json={
        "workflow_id": "needs-approval", "conversation_id": "c", "user_message": "go",
    }).json()["run"]["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body["run"]["status"] == "paused":
            return run_id, body["nodeRuns"][0]["id"]
        time.sleep(0.05)
    raise AssertionError("never paused")


def test_approver_recorded_in_event_and_metadata(client):
    run_id, nr_id = _paused_gate(client)
    for bad in ("x" * 129, 7):
        r = client.post(f"/runs/{run_id}/approve", json={"node_run_id": nr_id, "decision": "approved", "approved_by": bad})
        assert r.status_code == 400
    r = client.post(f"/runs/{run_id}/approve", json={"node_run_id": nr_id, "decision": "approved", "approved_by": "switchui"})
    assert r.status_code == 200

    body = client.get(f"/runs/{run_id}").json()
    meta = body["nodeRuns"][0]["metadata"]
    assert meta["approved_by"] == "switchui" and meta["decided_at"]
    received = [e for e in body["events"] if e["event_type"] == "approval_received"]
    assert received[0]["data"]["approved_by"] == "switchui"


def test_health_lists_b2_features(client):
    assert {"node_attempts", "approver", "node_retrying_event"} <= set(client.get("/health").json()["features"])
