"""
Tests for GET/POST /runs and GET /runs/{run_id}.
"""
from __future__ import annotations

import pytest
pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.wiring import create_engine

_HELLO_YAML = """\
id: hello-world
name: Hello World
description: A minimal test workflow
nodes:
  - id: greet
    prompt: Say hello
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
        # Seed a definition so runs can reference it
        c.post("/definitions", json={
            "id": "hello-world",
            "name": "Hello World",
            "yaml": _HELLO_YAML,
            "source": "user",
        })
        yield c

    api_mod._engine = original


def test_list_runs_empty(client):
    r = client.get("/runs")
    assert r.status_code == 200
    body = r.json()
    assert "runs" in body
    assert isinstance(body["runs"], list)


def test_create_run_success(client):
    r = client.post("/runs", json={
        "workflow_id": "hello-world",
        "conversation_id": "conv-001",
        "user_message": "start the workflow",
    })
    assert r.status_code == 201
    body = r.json()
    assert "run" in body
    run = body["run"]
    assert run["workflow_id"] == "hello-world"
    assert run["conversation_id"] == "conv-001"


def test_create_run_unknown_workflow(client):
    r = client.post("/runs", json={
        "workflow_id": "no-such-workflow",
        "conversation_id": "conv-002",
        "user_message": "go",
    })
    assert r.status_code == 404


def test_create_run_missing_fields(client):
    r = client.post("/runs", json={"workflow_id": "hello-world"})
    assert r.status_code == 400
    assert "required" in r.json()["error"]


def test_create_run_invalid_working_path(client):
    r = client.post("/runs", json={
        "workflow_id": "hello-world",
        "conversation_id": "conv-003",
        "user_message": "go",
        "working_path": "relative/path",
    })
    assert r.status_code == 400
    assert "working_path" in r.json()["error"]


def test_get_run_by_id(client):
    r = client.post("/runs", json={
        "workflow_id": "hello-world",
        "conversation_id": "conv-004",
        "user_message": "launch",
    })
    assert r.status_code == 201
    run_id = r.json()["run"]["id"]

    r2 = client.get(f"/runs/{run_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["run"]["id"] == run_id
    assert "nodeRuns" in body
    assert "events" in body


def test_get_run_not_found(client):
    r = client.get("/runs/nonexistent-run-id")
    assert r.status_code == 404


def test_list_runs_filter_by_workflow(client):
    client.post("/runs", json={
        "workflow_id": "hello-world",
        "conversation_id": "conv-005",
        "user_message": "go",
    })
    r = client.get("/runs?workflow_id=hello-world")
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert all(run["workflow_id"] == "hello-world" for run in runs)


def test_list_runs_filter_by_status(client):
    client.post("/runs", json={
        "workflow_id": "hello-world",
        "conversation_id": "conv-006",
        "user_message": "go",
    })
    # running or pending are valid statuses after start_run
    r = client.get("/runs?status=running,pending,completed")
    assert r.status_code == 200
    body = r.json()
    assert "runs" in body
