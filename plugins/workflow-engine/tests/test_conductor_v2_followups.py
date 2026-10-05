"""Conductor v2 B1/B2 review follow-ups: subgraph pins, attempt counters at
node start, tolerant approval metadata, snapshot warnings, lineage scope,
approver validation, snapshot retention."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.store.run_store import STORE_LOCK
from engine.wiring import create_engine

CHILD_V1 = """\
name: child
description: d
kind: subgraph
nodes:
  - id: inner_v1
    bash: echo v1
"""
CHILD_V2 = CHILD_V1.replace("inner_v1", "inner_v2").replace("echo v1", "echo v2")
PARENT = """\
name: parent
description: d
nodes:
  - id: gate
    approval:
      message: ok?
  - id: sub
    depends_on: [gate]
    subgraph:
      ref: child
"""
GATED = """\
name: gated
description: d
nodes:
  - id: gate
    approval:
      message: ok?
"""
ONE = """\
name: one
description: d
nodes:
  - id: a
    bash: echo a
"""


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
def client(eng):
    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    original = api_mod._engine
    api_mod._engine = lambda: eng
    app = FastAPI()
    app.include_router(api_mod.router)
    with TestClient(app) as c:
        yield c
    api_mod._engine = original


async def _wait(eng, run_id, statuses, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await eng.get_run(run_id)
        if r["status"] in statuses:
            return r
        await asyncio.sleep(0.05)
    raise AssertionError(f"run stuck in {r['status']}")


def _sql(eng, sql, args=()):
    with STORE_LOCK:
        rows = eng._conn.execute(sql, args).fetchall()
        eng._conn.commit()
    return rows


# 1. subgraph pinning ─────────────────────────────────────────────────────────

def test_subgraph_pinned_at_start_survives_edit(eng, client):
    async def go():
        await eng.upsert_definition("child", CHILD_V1)
        await eng.upsert_definition("parent", PARENT)
        run = await eng.start_run("parent", {}, {"kind": "manual", "conversation_id": "c"})
        await _wait(eng, run["id"], {"paused"})
        await eng.upsert_definition("child", CHILD_V2)  # edit while paused
        await eng.approve(run["id"], "gate", "approve")
        await _wait(eng, run["id"], {"completed"})
        return run["id"], {nr["dag_node_id"] for nr in await eng.list_node_runs(run["id"])}

    run_id, nodes = _arun(go())
    assert "sub.inner_v1" in nodes and "sub.inner_v2" not in nodes
    pins = client.get(f"/runs/{run_id}/definition").json()["definition"]["subgraphs_pinned"]
    snap = _sql(eng, "SELECT yaml FROM workflow_definition_snapshots WHERE workflow_id='child' AND checksum=?",
                (pins["child"],))
    assert list(pins) == ["child"] and snap[0]["yaml"] == CHILD_V1


# 2/3. attempt counters at node start ─────────────────────────────────────────

def test_attempt_counters_written_at_node_started(eng):
    yaml_text = """\
name: counters
description: d
nodes:
  - id: configured
    bash: echo ok
    retry: {max_attempts: 3, delay_ms: 1500}
  - id: defaults
    bash: echo ok
"""

    async def go():
        await eng.upsert_definition("counters", yaml_text)
        run = await eng.start_run("counters", {}, {"kind": "manual", "conversation_id": "c"})
        await _wait(eng, run["id"], {"completed"})
        return {r["dag_node_id"]: r for r in await eng.list_node_runs(run["id"])}

    rows = _arun(go())
    pick = lambda r: (r["retries"], r["max_retries"], r["retry_delay_ms"])  # noqa: E731
    assert pick(rows["configured"]) == (0, 3, 1500)
    assert pick(rows["defaults"]) == (0, 2, 3000)


# 4. approval metadata tolerates a corrupt row ────────────────────────────────

def test_approve_with_invalid_metadata(eng):
    async def go():
        await eng.upsert_definition("gated", GATED)
        run = await eng.start_run("gated", {}, {"kind": "manual", "conversation_id": "c"})
        await _wait(eng, run["id"], {"paused"})
        _sql(eng, "UPDATE node_runs SET metadata = 'not json' WHERE workflow_run_id = ?", (run["id"],))
        await eng.approve(run["id"], "gate", "approve", actor="ops")
        await _wait(eng, run["id"], {"completed"})
        return (await eng.list_node_runs(run["id"]))[0]

    nr = _arun(go())
    assert nr["status"] == "completed" and nr["metadata"]["approved_by"] == "ops"


# 5. missing snapshot warns; version comes from the run row ──────────────────

def test_missing_snapshot_warns_and_version_from_run(eng, client, caplog):
    assert client.post("/definitions", json={"id": "one", "name": "o", "yaml": ONE, "source": "user"}).status_code == 201
    run = client.post("/runs", json={"workflow_id": "one", "conversation_id": "c", "user_message": "go"}).json()["run"]
    _sql(eng, "UPDATE workflow_runs SET definition_version = 'v-run' WHERE id = ?", (run["id"],))
    assert client.get(f"/runs/{run['id']}/definition").json()["definition"]["version"] == "v-run"

    _sql(eng, "DELETE FROM workflow_definition_snapshots")
    with caplog.at_level(logging.WARNING, logger="workflow.engine"):
        d = client.get(f"/runs/{run['id']}/definition").json()["definition"]
    assert d["source"] == "current" and "snapshot" in caplog.text and "missing" in caplog.text


# 6. lineage stays inside one workflow; cron never carries it ────────────────

def test_parent_run_must_be_same_workflow(eng, client):
    for wf in ("one", "two"):
        client.post("/definitions", json={"id": wf, "name": wf, "yaml": ONE, "source": "user"})
    parent = client.post("/runs", json={"workflow_id": "one", "conversation_id": "c", "user_message": "go"}).json()["run"]
    r = client.post("/runs", json={"workflow_id": "two", "conversation_id": "c", "user_message": "go",
                                   "parent_run_id": parent["id"]})
    assert r.status_code == 400 and "different workflow" in r.json()["error"]


def test_cron_schedule_drops_parent_run_id(eng):
    pytest.importorskip("croniter")

    async def go():
        await eng.upsert_definition("one", ONE)
        return await eng.schedule_run("one", {}, {"kind": "manual", "parent_run_id": "p"},
                                      schedule={"type": "cron", "cron": "*/5 * * * *"})

    row = _arun(go())
    trig = json.loads(_sql(eng, "SELECT trigger_json FROM scheduled_runs WHERE id = ?", (row["id"],))[0][0])
    assert "parent_run_id" not in trig


# 7. approver label validation ───────────────────────────────────────────────

def test_approved_by_stripped_and_validated(eng, client):
    client.post("/definitions", json={"id": "gated", "name": "g", "yaml": GATED, "source": "user"})
    run = client.post("/runs", json={"workflow_id": "gated", "conversation_id": "c", "user_message": "go"}).json()["run"]
    _arun(_wait(eng, run["id"], {"paused"}))
    nr_id = _arun(eng.list_node_runs(run["id"]))[0]["id"]

    def approve(by):
        return client.post(f"/runs/{run['id']}/approve",
                           json={"node_run_id": nr_id, "decision": "approved", "approved_by": by})

    for bad in ("", "   ", "a\x00b", "line\nbreak", 7, "x" * 129):
        assert approve(bad).status_code == 400, bad
    assert approve("  ops  ").status_code == 200
    nr = _arun(eng.list_node_runs(run["id"]))[0]
    assert nr["metadata"]["approved_by"] == "ops"


# 8. retention drops snapshots no run references ─────────────────────────────

def test_retention_drops_unreferenced_snapshots(eng):
    store = eng._run_store
    _sql(eng, "INSERT INTO workflow_definitions (id, name, source, yaml, checksum, created_at, updated_at) "
              "VALUES ('wf', 'wf', 'user', 'x', 'c', 1, 1)")
    for wf, ck in (("wf", "kept-def"), ("sub", "kept-sub"), ("wf", "orphan")):
        store.insert_definition_snapshot(workflow_id=wf, checksum=ck, version=None, yaml="x")
    _sql(eng, "UPDATE workflow_definition_snapshots SET created_at = 1")
    run = store.create_workflow_run(workflow_id="wf", conversation_id="c", working_path="/tmp",
                                    user_message="m", definition_checksum="kept-def")
    store.update_workflow_run(run["id"], metadata={"subgraph_pins": {"sub": "kept-sub"}})
    store.delete_terminal_runs_older_than(1)
    left = {r[0] for r in _sql(eng, "SELECT checksum FROM workflow_definition_snapshots")}
    assert left == {"kept-def", "kept-sub"}
