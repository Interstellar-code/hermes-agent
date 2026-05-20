"""
Tests for GET/POST /definitions and GET /definitions/{id}/parsed.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

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
    api_mod._engine = engine
    app.include_router(api_mod.router)

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c

    api_mod._engine = original


def test_list_definitions_empty(client):
    r = client.get("/definitions")
    assert r.status_code == 200
    body = r.json()
    assert "definitions" in body
    assert isinstance(body["definitions"], list)


def test_create_and_get_definition(client):
    r = client.post("/definitions", json={
        "id": "hello-world",
        "name": "Hello World",
        "yaml": _HELLO_YAML,
        "source": "user",
    })
    assert r.status_code == 201
    body = r.json()
    assert "definition" in body
    assert body["definition"]["id"] == "hello-world"

    r2 = client.get("/definitions/hello-world")
    assert r2.status_code == 200
    assert r2.json()["definition"]["id"] == "hello-world"


def test_get_definition_not_found(client):
    r = client.get("/definitions/does-not-exist")
    assert r.status_code == 404


def test_create_definition_invalid_id(client):
    r = client.post("/definitions", json={
        "id": "bad id!",
        "name": "Test",
        "yaml": _HELLO_YAML,
    })
    assert r.status_code == 400
    assert "id" in r.json()["error"]


def test_create_definition_bundled_readonly(client):
    r = client.post("/definitions", json={
        "id": "hello-world",
        "name": "Hello World",
        "yaml": _HELLO_YAML,
        "source": "bundled",
    })
    assert r.status_code == 403


def test_get_definition_parsed(client):
    client.post("/definitions", json={
        "id": "hello-world",
        "name": "Hello World",
        "yaml": _HELLO_YAML,
        "source": "user",
    })
    r = client.get("/definitions/hello-world/parsed")
    assert r.status_code == 200
    body = r.json()
    assert "parsed" in body
    assert body["parsed"]["id"] == "hello-world"


def test_get_parsed_not_found(client):
    r = client.get("/definitions/nonexistent/parsed")
    assert r.status_code == 404


def test_list_definitions_after_create(client):
    client.post("/definitions", json={
        "id": "hello-world",
        "name": "Hello World",
        "yaml": _HELLO_YAML,
        "source": "user",
    })
    r = client.get("/definitions")
    assert r.status_code == 200
    defs = r.json()["definitions"]
    assert any(d["id"] == "hello-world" for d in defs)
