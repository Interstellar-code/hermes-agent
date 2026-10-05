"""Conductor v2 B1: migration 009, definition pinning, run lineage."""
from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.db.client import open_db
from engine.db.migrate import ensure_schema
from engine.store.run_store import STORE_LOCK, RunStore
from engine.wiring import create_engine

V1 = """\
name: pin-me
description: d
nodes:
  - id: one
    bash: echo v1
"""
V2 = """\
name: pin-me
description: d
nodes:
  - id: two
    bash: echo v2
"""
GATED_V1 = """\
name: gated
description: d
nodes:
  - id: gate
    approval:
      message: ok?
  - id: after_v1
    bash: echo v1
    depends_on: [gate]
"""
GATED_V2 = GATED_V1.replace("after_v1", "after_v2")

_NEW_RUN_COLS = {"parent_run_id", "definition_checksum", "definition_version"}
_NEW_INDEXES = {"idx_wr_parent", "idx_wr_started_id", "idx_we_run_node"}


def _arun(coro):
    # Not asyncio.run: it clears the thread's current loop, which breaks
    # later tests that call asyncio.get_event_loop().
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _version(conn) -> str:
    return conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]


def _objects(conn) -> set:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}


# ── migration ───────────────────────────────────────────────────────────────

def test_fresh_db_migrates_to_009():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        assert int(_version(conn)) >= 9
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(workflow_runs)")}
        assert _NEW_RUN_COLS <= cols
        assert {"workflow_definition_snapshots", *_NEW_INDEXES} <= _objects(conn)


def test_008_db_with_runs_migrates_and_old_runs_serialize():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        # Roll back to an 008 shape holding a pre-009 run.
        for idx in _NEW_INDEXES:
            conn.execute(f"DROP INDEX {idx}")
        conn.execute("DROP TABLE workflow_definition_snapshots")
        for col in _NEW_RUN_COLS:
            conn.execute(f"ALTER TABLE workflow_runs DROP COLUMN {col}")
        conn.execute("UPDATE schema_meta SET value='8' WHERE key='schema_version'")
        conn.execute(
            "INSERT INTO workflow_definitions (id, name, source, yaml, checksum, created_at, updated_at) "
            "VALUES ('old', 'old', 'user', 'x', 'c', 1, 1)"
        )
        conn.execute(
            "INSERT INTO workflow_runs (id, workflow_id, conversation_id, working_path, user_message, "
            "started_at, last_heartbeat) VALUES ('r-old', 'old', 'c', '/tmp', 'm', 1, 1)"
        )
        conn.commit()

        ensure_schema(conn)
        assert _version(conn) == "9"
        run = RunStore(conn).get_workflow_run("r-old")
        assert run["parent_run_id"] is None and run["definition_checksum"] is None

        # Half-applied (one column already there) converges; re-run is a no-op.
        conn.execute("DROP INDEX idx_wr_parent")
        conn.execute("ALTER TABLE workflow_runs DROP COLUMN definition_version")
        conn.execute("UPDATE schema_meta SET value='8' WHERE key='schema_version'")
        conn.commit()
        ensure_schema(conn)
        ensure_schema(conn)
        assert _version(conn) == "9"
        assert "idx_wr_parent" in _objects(conn)


# ── API ─────────────────────────────────────────────────────────────────────

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
        assert c.post("/definitions", json={"id": "pin-me", "name": "p", "yaml": V1, "source": "user"}).status_code == 201
        yield c
    api_mod._engine = original


def _start(client, **extra):
    r = client.post("/runs", json={"workflow_id": "pin-me", "conversation_id": "c", "user_message": "go", **extra})
    assert r.status_code == 201, r.text
    return r.json()["run"]


def test_health_lists_b1_features(client):
    assert {"definition_pin", "parent_run"} <= set(client.get("/health").json()["features"])


def test_run_definition_stays_pinned_after_edit(client):
    run = _start(client)
    assert run["definition_checksum"]
    assert client.post("/definitions", json={"id": "pin-me", "name": "p", "yaml": V2, "source": "user"}).status_code == 201

    body = client.get(f"/runs/{run['id']}/definition").json()
    d = body["definition"]
    assert d["yaml"] == V1 and d["source"] == "snapshot" and d["pinned"] is True
    assert d["checksum"] == run["definition_checksum"] != d["current_checksum"]
    assert d["current_updated_at"]
    assert [n["id"] for n in body["parsed"]["nodes"]] == ["one"]

    assert client.get("/runs/nope/definition").status_code == 404


def test_pre_009_run_reports_unpinned(client, eng):
    run = _start(client)
    with STORE_LOCK:
        eng._conn.execute("UPDATE workflow_runs SET definition_checksum = NULL WHERE id = ?", (run["id"],))
        eng._conn.commit()
    d = client.get(f"/runs/{run['id']}/definition").json()["definition"]
    assert d["pinned"] is False and d["source"] == "current" and d["yaml"] == V1


def test_parent_run_id_validated_stored_and_filterable(client):
    parent = _start(client)
    assert client.post("/runs", json={
        "workflow_id": "pin-me", "conversation_id": "c", "user_message": "go", "parent_run_id": "missing",
    }).json() == {"error": "parent_run_id not found"}
    child = _start(client, parent_run_id=parent["id"])
    assert child["parent_run_id"] == parent["id"]
    assert [r["id"] for r in client.get(f"/runs?parent_run_id={parent['id']}").json()["runs"]] == [child["id"]]


# ── resume ──────────────────────────────────────────────────────────────────

async def _wait(eng, run_id, statuses):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        r = await eng.get_run(run_id)
        if r["status"] in statuses:
            return r
        await asyncio.sleep(0.05)
    raise AssertionError(f"run stuck in {r['status']}")


def test_resume_uses_pinned_yaml(eng):
    async def go():
        await eng.upsert_definition("gated", GATED_V1)
        run = await eng.start_run("gated", {}, {"kind": "manual", "conversation_id": "c"})
        await _wait(eng, run["id"], {"paused"})
        await eng.upsert_definition("gated", GATED_V2)  # edit while paused
        await eng.approve(run["id"], "gate", "approve")
        await _wait(eng, run["id"], {"completed"})
        return {nr["dag_node_id"] for nr in await eng.list_node_runs(run["id"])}

    assert _arun(go()) == {"gate", "after_v1"}
