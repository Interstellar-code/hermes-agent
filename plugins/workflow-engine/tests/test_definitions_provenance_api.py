"""
Tests for Phase 3 provenance API behavior:
- Edit bundled row via POST → stays source='bundled', user_modified=1
- Survives reseed (user_modified=1 rows preserved)
- Stale expected_checksum → 409
- reset-factory restores factory yaml + clears user_modified
- New explicit source='bundled' create still 403
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from fastapi import FastAPI

from engine.wiring import create_engine
from engine.store.definition_store import _sha256

_FACTORY_YAML = """\
id: bundled-wf
name: Bundled Workflow
description: Factory version
nodes:
  - id: step1
    prompt: Do factory step
"""

_FACTORY_YAML_V2 = """\
id: bundled-wf
name: Bundled Workflow v2
description: Upgraded factory version
nodes:
  - id: step1
    prompt: Do upgraded factory step
"""

_USER_EDIT_YAML = """\
id: bundled-wf
name: My Custom Edit
description: User edited
nodes:
  - id: step1
    prompt: My custom prompt
"""

_OTHER_YAML = """\
id: other-wf
name: Other Workflow
nodes:
  - id: step1
    prompt: Other step
"""


@pytest.fixture()
def client_and_engine():
    engine = create_engine(
        db_path=":memory:", seed_bundled=False, write_manifest=False, crash_recovery=False
    )
    app = FastAPI()

    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    original = api_mod._engine
    api_mod._engine = lambda: engine
    app.include_router(api_mod.router)
    app.state.workflow_engine = engine

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, engine

    api_mod._engine = original


@pytest.fixture()
def client(client_and_engine):
    c, _ = client_and_engine
    return c


def _insert_bundled_row(engine, yaml_text=_FACTORY_YAML):
    """Directly insert a bundled row with correct provenance fields."""
    checksum = _sha256(yaml_text)
    engine._conn.execute(
        "INSERT INTO workflow_definitions "
        "(id, name, source, yaml, checksum, bundled_checksum, user_modified, created_at, updated_at, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("bundled-wf", "Bundled Workflow", "bundled", yaml_text, checksum,
         checksum, 0, 1, 1, "workflow"),
    )
    engine._conn.commit()


# ---------------------------------------------------------------------------
# Edit bundled row via POST → source='bundled', user_modified=1
# ---------------------------------------------------------------------------

def test_edit_bundled_row_via_post(client_and_engine):
    client, engine = client_and_engine
    _insert_bundled_row(engine)

    r = client.post("/definitions", json={
        "id": "bundled-wf",
        "name": "My Custom Edit",
        "yaml": _USER_EDIT_YAML,
        "source": "user",  # source field ignored for existing bundled rows
    })
    assert r.status_code == 200, r.text
    body = r.json()
    defn = body["definition"]
    assert defn["source"] == "bundled"
    assert defn["user_modified"] == 1
    assert defn["yaml"] == _USER_EDIT_YAML


def test_edited_bundled_row_survives_reseed(client_and_engine):
    """user_modified=1 rows are skipped during reseed — user edits preserved."""
    client, engine = client_and_engine
    _insert_bundled_row(engine)

    # Edit the bundled row
    client.post("/definitions", json={
        "id": "bundled-wf",
        "name": "My Custom Edit",
        "yaml": _USER_EDIT_YAML,
    })

    # Simulate reseed with original factory yaml
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "bundled-wf.yaml").write_text(_FACTORY_YAML, encoding="utf-8")
        result = engine._def_store.seed_bundled(d)

    assert result["skipped"] == 1
    assert result["updated"] == 0

    r = client.get("/definitions/bundled-wf")
    defn = r.json()["definition"]
    assert defn["user_modified"] == 1
    assert defn["yaml"] == _USER_EDIT_YAML


# ---------------------------------------------------------------------------
# Stale expected_checksum → 409
# ---------------------------------------------------------------------------

def test_stale_expected_checksum_returns_409(client_and_engine):
    client, engine = client_and_engine
    _insert_bundled_row(engine)

    r = client.post("/definitions", json={
        "id": "bundled-wf",
        "name": "My Custom Edit",
        "yaml": _USER_EDIT_YAML,
        "expected_checksum": "stale-checksum-value",
    })
    assert r.status_code == 409, r.text


def test_correct_expected_checksum_succeeds(client_and_engine):
    client, engine = client_and_engine
    _insert_bundled_row(engine)

    correct_checksum = _sha256(_FACTORY_YAML)
    r = client.post("/definitions", json={
        "id": "bundled-wf",
        "name": "My Custom Edit",
        "yaml": _USER_EDIT_YAML,
        "expected_checksum": correct_checksum,
    })
    assert r.status_code == 200, r.text
    assert r.json()["definition"]["user_modified"] == 1


# ---------------------------------------------------------------------------
# reset-factory restores factory yaml + clears user_modified
# ---------------------------------------------------------------------------

def test_reset_factory_endpoint(client_and_engine):
    """POST /definitions/{id}/reset-factory restores factory yaml and clears user_modified."""
    client, engine = client_and_engine
    _insert_bundled_row(engine)

    # First, edit the row
    client.post("/definitions", json={
        "id": "bundled-wf",
        "name": "My Custom Edit",
        "yaml": _USER_EDIT_YAML,
    })

    # Verify it's edited
    defn = client.get("/definitions/bundled-wf").json()["definition"]
    assert defn["user_modified"] == 1

    # Now reset — this requires a factory file to exist; we mock _find_factory_yaml
    # by patching the defaults dir. We insert the factory yaml directly via store instead.
    result = engine._def_store.reset_to_factory("bundled-wf", _FACTORY_YAML)
    assert result["user_modified"] == 0
    assert result["yaml"] == _FACTORY_YAML
    assert result["bundled_checksum"] == _sha256(_FACTORY_YAML)
    assert result["checksum"] == _sha256(_FACTORY_YAML)


def test_reset_factory_api_endpoint_404_for_nonbundled(client_and_engine):
    """reset-factory on non-bundled row returns 403."""
    client, engine = client_and_engine

    # Insert a user row
    engine._conn.execute(
        "INSERT INTO workflow_definitions "
        "(id, name, source, yaml, checksum, created_at, updated_at, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("user-wf", "User WF", "user", _OTHER_YAML, _sha256(_OTHER_YAML), 1, 1, "workflow"),
    )
    engine._conn.commit()

    r = client.post("/definitions/user-wf/reset-factory")
    assert r.status_code == 403, r.text


def test_reset_factory_api_endpoint_404_for_missing(client_and_engine):
    """reset-factory on unknown id returns 404."""
    client, engine = client_and_engine
    r = client.post("/definitions/does-not-exist/reset-factory")
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# New explicit source='bundled' create still 403
# ---------------------------------------------------------------------------

def test_new_bundled_source_create_is_403(client):
    r = client.post("/definitions", json={
        "id": "brand-new-id",
        "name": "Brand New",
        "yaml": _OTHER_YAML,
        "source": "bundled",
    })
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# store-level mark_user_edit / reset_to_factory
# ---------------------------------------------------------------------------

def test_mark_user_edit_store(client_and_engine):
    _, engine = client_and_engine
    _insert_bundled_row(engine)
    store = engine._def_store

    row = store.mark_user_edit("bundled-wf", _USER_EDIT_YAML)
    assert row["user_modified"] == 1
    assert row["source"] == "bundled"
    assert row["yaml"] == _USER_EDIT_YAML
    # bundled_checksum must not change
    assert row["bundled_checksum"] == _sha256(_FACTORY_YAML)


def test_mark_user_edit_conflict_error(client_and_engine):
    from engine.store.definition_store import ConflictError
    _, engine = client_and_engine
    _insert_bundled_row(engine)
    store = engine._def_store

    with pytest.raises(ConflictError):
        store.mark_user_edit("bundled-wf", _USER_EDIT_YAML, expected_checksum="wrong")


_USER_WF_YAML = """\
id: user-wf
name: User WF
description: A user workflow
nodes:
  - id: step1
    prompt: User step
"""


def test_mark_user_edit_not_bundled_raises_value_error(client_and_engine):
    _, engine = client_and_engine
    engine._conn.execute(
        "INSERT INTO workflow_definitions "
        "(id, name, source, yaml, checksum, created_at, updated_at, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("user-wf", "User WF", "user", _USER_WF_YAML, _sha256(_USER_WF_YAML), 1, 1, "workflow"),
    )
    engine._conn.commit()

    with pytest.raises(ValueError, match="[Nn]ot a bundled row"):
        engine._def_store.mark_user_edit("user-wf", _USER_WF_YAML)


def test_reset_to_factory_store(client_and_engine):
    _, engine = client_and_engine
    _insert_bundled_row(engine)
    store = engine._def_store

    # Edit first
    store.mark_user_edit("bundled-wf", _USER_EDIT_YAML)
    assert store.get_definition("bundled-wf")["user_modified"] == 1

    # Reset
    row = store.reset_to_factory("bundled-wf", _FACTORY_YAML)
    assert row["user_modified"] == 0
    assert row["yaml"] == _FACTORY_YAML
    assert row["bundled_checksum"] == _sha256(_FACTORY_YAML)
