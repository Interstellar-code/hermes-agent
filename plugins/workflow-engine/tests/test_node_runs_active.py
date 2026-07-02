"""GET /node-runs/active — empty, populated, and literal-vs-dynamic regression."""
from __future__ import annotations

import pytest
pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.wiring import create_engine


@pytest.fixture()
def client_and_engine():
    engine = create_engine(
        db_path=":memory:",
        seed_bundled=False,
        write_manifest=False,
        crash_recovery=False,
    )
    app = FastAPI()

    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    original = api_mod._engine
    api_mod._engine = lambda: engine
    app.include_router(api_mod.router)

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, engine

    api_mod._engine = original


def test_node_runs_active_empty(client_and_engine):
    c, _ = client_and_engine
    r = c.get("/node-runs/active")
    assert r.status_code == 200
    assert r.json() == {"nodeRuns": []}


def _seed_run_and_node(engine, status: str = "running"):
    """Insert a workflow_run and a node_run directly via SQL for test setup."""
    conn = engine._conn
    conn.execute(
        "INSERT INTO workflow_definitions "
        "(id, name, source, yaml, checksum, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("wf-1", "WF1", "user", "id: wf-1\nname: WF1\nnodes: []\n", "x", 1, 1),
    )
    conn.execute(
        "INSERT INTO workflow_runs "
        "(id, workflow_id, conversation_id, working_path, user_message, "
        "status, current_phase, started_at, last_heartbeat) "
        "VALUES (?, ?, ?, ?, ?, 'running', 'plan', ?, ?)",
        ("run-1", "wf-1", "c1", "/tmp", "go", 100, 100),
    )
    conn.execute(
        "INSERT INTO node_runs "
        "(id, workflow_run_id, dag_node_id, node_type, status, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("nr-1", "run-1", "node-a", "prompt", status, 200),
    )
    conn.commit()


def test_node_runs_active_populated(client_and_engine):
    c, engine = client_and_engine
    _seed_run_and_node(engine, status="running")
    r = c.get("/node-runs/active")
    assert r.status_code == 200
    body = r.json()
    assert len(body["nodeRuns"]) == 1
    nr = body["nodeRuns"][0]
    assert nr["runId"] == "run-1"
    assert nr["nodeId"] == "node-a"
    assert nr["workflowId"] == "wf-1"
    assert nr["status"] == "running"
    assert nr["workerId"] is None
    assert "startedAt" in nr


def test_node_runs_active_ignores_terminal(client_and_engine):
    c, engine = client_and_engine
    _seed_run_and_node(engine, status="completed")
    r = c.get("/node-runs/active")
    assert r.json() == {"nodeRuns": []}


def test_literal_active_does_not_match_dynamic_route(client_and_engine):
    """Regression: /node-runs/active must NOT be captured by /node-runs/{id}."""
    c, _ = client_and_engine
    # Empty store => the literal route returns {"nodeRuns": []} (200), while the
    # dynamic route would return 404. Reaching 200 here proves order is right.
    r = c.get("/node-runs/active")
    assert r.status_code == 200
    assert "nodeRuns" in r.json()
    # And the dynamic route still resolves for an unknown id.
    r2 = c.get("/node-runs/some-unknown-id")
    assert r2.status_code == 404
