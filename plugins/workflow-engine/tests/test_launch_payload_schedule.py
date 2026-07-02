"""POST /runs — new schedule / priority / maxRuntimeSeconds payload fields."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

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
        c.post("/definitions", json={
            "id": "hello-world",
            "name": "Hello World",
            "yaml": _HELLO_YAML,
            "source": "user",
        })
        yield c
    api_mod._engine = original


def _base_payload(**extra):
    p = {
        "workflow_id": "hello-world",
        "conversation_id": "conv-launch",
        "user_message": "go",
    }
    p.update(extra)
    return p


def test_now_regression_no_extra_fields(client):
    """No schedule / no priority / no maxRuntimeSeconds → same as old behaviour."""
    r = client.post("/runs", json=_base_payload())
    assert r.status_code == 201
    body = r.json()
    assert "run" in body
    assert body["run"]["workflow_id"] == "hello-world"
    # Old shape preserved: 'id' exists.
    assert "id" in body["run"]


def test_explicit_schedule_now(client):
    r = client.post("/runs", json=_base_payload(schedule={"type": "now"}))
    assert r.status_code == 201
    assert "run" in r.json()


def test_schedule_at_returns_scheduled(client):
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()
    r = client.post("/runs", json=_base_payload(
        schedule={"type": "at", "at": future},
    ))
    assert r.status_code == 201
    body = r.json()["run"]
    assert body["status"] == "scheduled"
    assert body["scheduled_for"] == future
    assert "id" in body


def test_schedule_cron_returns_501(client):
    r = client.post("/runs", json=_base_payload(
        schedule={"type": "cron", "cron_expr": "*/5 * * * *"},
    ))
    assert r.status_code == 501
    assert "cron" in r.json()["error"].lower()


def test_schedule_at_requires_at_string(client):
    r = client.post("/runs", json=_base_payload(schedule={"type": "at"}))
    assert r.status_code == 400


def test_invalid_schedule_type(client):
    r = client.post("/runs", json=_base_payload(schedule={"type": "tomorrow"}))
    assert r.status_code == 400


def test_schedule_not_object(client):
    r = client.post("/runs", json=_base_payload(schedule="now"))
    assert r.status_code == 400


def test_priority_lower_bound(client):
    r = client.post("/runs", json=_base_payload(priority=-101))
    assert r.status_code == 400


def test_priority_upper_bound(client):
    r = client.post("/runs", json=_base_payload(priority=101))
    assert r.status_code == 400


def test_priority_within_range(client):
    r = client.post("/runs", json=_base_payload(priority=50))
    assert r.status_code == 201


def test_priority_must_be_int(client):
    r = client.post("/runs", json=_base_payload(priority="high"))
    assert r.status_code == 400


def test_max_runtime_zero_rejected(client):
    r = client.post("/runs", json=_base_payload(maxRuntimeSeconds=0))
    assert r.status_code == 400


def test_max_runtime_over_cap_rejected(client):
    r = client.post("/runs", json=_base_payload(maxRuntimeSeconds=86401))
    assert r.status_code == 400


def test_max_runtime_accepted(client):
    r = client.post("/runs", json=_base_payload(maxRuntimeSeconds=60))
    assert r.status_code == 201


def test_max_runtime_must_be_int(client):
    r = client.post("/runs", json=_base_payload(maxRuntimeSeconds="60"))
    assert r.status_code == 400
