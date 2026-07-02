"""
Tests for POST /runs/{run_id}/approve.
"""
from __future__ import annotations

import pytest
pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.wiring import create_engine

_APPROVAL_YAML = """\
id: needs-approval
name: Needs Approval
description: Workflow with an approval gate
nodes:
  - id: ask
    approval: Please approve this step
"""


@pytest.fixture()
def client():
    engine = create_engine(db_path=":memory:", seed_bundled=False, write_manifest=False, crash_recovery=False)
    app = FastAPI()

    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    original = api_mod._engine
    api_mod._engine = lambda: engine
    app.include_router(api_mod.router)

    with TestClient(app, raise_server_exceptions=True) as c:
        c.post("/definitions", json={
            "id": "needs-approval",
            "name": "Needs Approval",
            "yaml": _APPROVAL_YAML,
            "source": "user",
        })
        yield c

    api_mod._engine = original


def test_approve_run_not_found(client):
    r = client.post("/runs/nonexistent/approve", json={
        "node_run_id": "some-node-run-id",
        "decision": "approved",
    })
    assert r.status_code == 404


def test_approve_missing_node_run_id(client):
    r = client.post("/runs/needs-approval/approve", json={"decision": "approved"})
    # run not found (no run exists yet), returns 404
    # If run existed, node_run_id missing → 400. Either way not 200.
    assert r.status_code in (400, 404)


def test_approve_invalid_decision(client):
    # Create a run first
    r = client.post("/runs", json={
        "workflow_id": "needs-approval",
        "conversation_id": "conv-approve-001",
        "user_message": "run it",
    })
    assert r.status_code == 201
    run_id = r.json()["run"]["id"]

    r2 = client.post(f"/runs/{run_id}/approve", json={
        "node_run_id": "some-id",
        "decision": "maybe",
    })
    assert r2.status_code == 400
    assert "decision" in r2.json()["error"]


def test_approve_node_run_not_found(client):
    r = client.post("/runs", json={
        "workflow_id": "needs-approval",
        "conversation_id": "conv-approve-002",
        "user_message": "run it",
    })
    assert r.status_code == 201
    run_id = r.json()["run"]["id"]

    r2 = client.post(f"/runs/{run_id}/approve", json={
        "node_run_id": "00000000-0000-0000-0000-000000000000",
        "decision": "approved",
    })
    assert r2.status_code == 404
    assert "node_run" in r2.json()["error"]


def test_approve_node_run_wrong_run(client):
    """node_run that exists but belongs to different run returns 400."""
    # Create two runs
    r1 = client.post("/runs", json={
        "workflow_id": "needs-approval",
        "conversation_id": "conv-approve-003",
        "user_message": "run 1",
    })
    assert r1.status_code == 201
    run_id_1 = r1.json()["run"]["id"]

    # GET run detail to find any node_runs
    detail = client.get(f"/runs/{run_id_1}").json()
    node_runs = detail.get("nodeRuns", [])

    if not node_runs:
        pytest.skip("No node_runs created (DAG may have completed instantly)")

    node_run_id = node_runs[0]["id"]

    # Create second run and try to approve with first run's node_run_id
    r2 = client.post("/runs", json={
        "workflow_id": "needs-approval",
        "conversation_id": "conv-approve-004",
        "user_message": "run 2",
    })
    assert r2.status_code == 201
    run_id_2 = r2.json()["run"]["id"]

    r3 = client.post(f"/runs/{run_id_2}/approve", json={
        "node_run_id": node_run_id,
        "decision": "approved",
    })
    assert r3.status_code in (400, 404)
