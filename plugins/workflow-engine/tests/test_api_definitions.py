"""
Tests for GET/POST /definitions and GET /definitions/{id}/parsed.
"""
from __future__ import annotations

import pytest
pytest.importorskip("fastapi")

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

_NO_ID_YAML = """\
name: Hello World
description: A minimal test workflow
nodes:
  - id: greet
    prompt: Say hello
"""

_OTHER_ID_YAML = """\
id: yaml-id
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
    app.state.workflow_engine = engine

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


def test_list_definitions_source_user_filters_out_bundled(client):
    client.post("/definitions", json={
        "id": "user-flow",
        "name": "User Flow",
        "yaml": _NO_ID_YAML,
        "source": "user",
    })
    client.post("/definitions", json={
        "id": "project-flow",
        "name": "Project Flow",
        "yaml": _NO_ID_YAML,
        "source": "project",
    })

    bundled_yaml = """\
id: bundled-flow
name: Bundled Flow
description: bundled
nodes:
  - id: greet
    prompt: Say hello
"""
    engine = client.app.state.workflow_engine
    engine._def_store._conn.execute(
        """INSERT INTO workflow_definitions
             (id, name, description, source, yaml, checksum, created_at, updated_at, kind)
           VALUES (?, ?, ?, 'bundled', ?, 'test', 1, 1, 'workflow')""",
        ("bundled-flow", "Bundled Flow", "Bundled Flow", bundled_yaml),
    )
    engine._def_store._conn.commit()

    r = client.get("/definitions?source=user")
    assert r.status_code == 200
    defs = r.json()["definitions"]
    ids = {d["id"] for d in defs}
    assert "user-flow" in ids
    assert "project-flow" not in ids
    assert "bundled-flow" not in ids


def test_list_definitions_source_system_maps_to_bundled(client):
    bundled_yaml = """\
id: bundled-flow
name: Bundled Flow
description: bundled
nodes:
  - id: greet
    prompt: Say hello
"""
    engine = client.app.state.workflow_engine
    engine._def_store._conn.execute(
        """INSERT INTO workflow_definitions
             (id, name, description, source, yaml, checksum, created_at, updated_at, kind)
           VALUES (?, ?, ?, 'bundled', ?, 'test', 1, 1, 'workflow')""",
        ("bundled-flow", "Bundled Flow", "Bundled Flow", bundled_yaml),
    )
    engine._def_store._conn.commit()

    r = client.get("/definitions?source=system")
    assert r.status_code == 200
    defs = r.json()["definitions"]
    ids = {d["id"] for d in defs}
    assert ids == {"bundled-flow"}


def test_list_definitions_source_all_returns_everything(client):
    client.post("/definitions", json={
        "id": "user-flow",
        "name": "User Flow",
        "yaml": _NO_ID_YAML,
        "source": "user",
    })
    bundled_yaml = """\
id: bundled-flow
name: Bundled Flow
description: bundled
nodes:
  - id: greet
    prompt: Say hello
"""
    engine = client.app.state.workflow_engine
    engine._def_store._conn.execute(
        """INSERT INTO workflow_definitions
             (id, name, description, source, yaml, checksum, created_at, updated_at, kind)
           VALUES (?, ?, ?, 'bundled', ?, 'test', 1, 1, 'workflow')""",
        ("bundled-flow", "Bundled Flow", "Bundled Flow", bundled_yaml),
    )
    engine._def_store._conn.commit()

    r = client.get("/definitions?source=all")
    assert r.status_code == 200
    defs = r.json()["definitions"]
    ids = {d["id"] for d in defs}
    assert {"user-flow", "bundled-flow"} <= ids


def test_list_definitions_invalid_source_returns_400(client):
    r = client.get("/definitions?source=nope")
    assert r.status_code == 400
    assert "source must be one of" in r.json()["error"]


def test_create_definition_body_id_wins_when_yaml_has_no_id(client):
    r = client.post("/definitions", json={
        "id": "body-id",
        "name": "Hello World",
        "yaml": _NO_ID_YAML,
        "source": "user",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["definition"]["id"] == "body-id"

    r2 = client.get("/definitions/body-id")
    assert r2.status_code == 200
    assert r2.json()["definition"]["id"] == "body-id"


def test_create_definition_body_id_overrides_yaml_id(client):
    r = client.post("/definitions", json={
        "id": "body-id",
        "name": "Hello World",
        "yaml": _OTHER_ID_YAML,
        "source": "user",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["definition"]["id"] == "body-id"

    missing = client.get("/definitions/yaml-id")
    assert missing.status_code == 404
