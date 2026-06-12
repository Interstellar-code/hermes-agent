"""REST tests for kanban template routes in plugins/kanban/dashboard/plugin_api.py.

Tests all 7 template routes:
  GET  /templates
  GET  /templates/{slug}
  POST /templates          (YAML body + JSON body variants)
  PUT  /templates/{slug}
  DELETE /templates/{slug}
  POST /templates/{slug}/instantiate

Error cases: 413 oversized body, 422 bad slug / invalid body, 404 missing, 409 cap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.kanban_db as kb
from hermes_cli import kanban_templates as kt


# ---------------------------------------------------------------------------
# Fixture: isolated home + FastAPI test app with the kanban router mounted
# ---------------------------------------------------------------------------

@pytest.fixture()
def template_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with empty kanban + templates directories."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


@pytest.fixture()
def client(template_home):
    """TestClient wrapping just the kanban plugin APIRouter mounted on a fresh FastAPI app."""
    from plugins.kanban.dashboard.plugin_api import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Minimal YAML fixtures
# ---------------------------------------------------------------------------

_MINIMAL_YAML = """\
schema: 1
name: Test Template
tasks:
  - key: a
    title: "Task A"
  - key: b
    title: "Task B"
"""

_YAML_WITH_BOARD = """\
schema: 1
name: Board Template
board:
  slug: rest-board
tasks:
  - key: t1
    title: "Task 1"
  - key: t2
    title: "Task 2"
links:
  - [t1, t2]
"""


# ---------------------------------------------------------------------------
# GET /templates — list
# ---------------------------------------------------------------------------

class TestListTemplates:
    def test_empty_list(self, client):
        r = client.get("/templates")
        assert r.status_code == 200
        body = r.json()
        assert body["templates"] == []

    def test_returns_saved_template(self, client, template_home):
        kt.save_template("my-tmpl", _MINIMAL_YAML)
        r = client.get("/templates")
        assert r.status_code == 200
        slugs = [t["slug"] for t in r.json()["templates"]]
        assert "my-tmpl" in slugs


# ---------------------------------------------------------------------------
# GET /templates/{slug} — get single
# ---------------------------------------------------------------------------

class TestGetTemplate:
    def test_happy_path(self, client, template_home):
        kt.save_template("my-tmpl", _MINIMAL_YAML)
        r = client.get("/templates/my-tmpl")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Test Template"
        assert len(body["tasks"]) == 2

    def test_404_for_missing(self, client):
        r = client.get("/templates/no-such-template")
        assert r.status_code == 404

    def test_422_for_bad_slug(self, client):
        r = client.get("/templates/UPPER_CASE")
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /templates — create (YAML body)
# ---------------------------------------------------------------------------

class TestCreateTemplateYamlBody:
    def test_yaml_body_creates_template(self, client):
        yaml_body = f"slug: rest-tmpl\n{_MINIMAL_YAML}"
        r = client.post(
            "/templates",
            content=yaml_body,
            headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 201
        body = r.json()
        assert "template" in body
        assert body["template"]["name"] == "Test Template"

    def test_json_body_creates_template(self, client):
        payload = {"slug": "json-tmpl", "yaml": _MINIMAL_YAML}
        r = client.post(
            "/templates",
            json=payload,
        )
        assert r.status_code == 201
        assert r.json()["template"]["name"] == "Test Template"

    def test_413_oversized_body(self, client):
        big = "x" * (kt.MAX_TEMPLATE_BYTES + 1)
        r = client.post(
            "/templates",
            content=big,
            headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 413

    def test_422_missing_yaml_content(self, client):
        r = client.post(
            "/templates",
            json={"slug": "no-yaml"},
        )
        assert r.status_code == 422

    def test_422_invalid_yaml(self, client):
        r = client.post(
            "/templates",
            content="slug: bad-yaml\nschema: 1\ntasks:\n  - key: [unclosed",
            headers={"Content-Type": "text/yaml"},
        )
        # validation error from bad YAML
        assert r.status_code in (422, 400)

    def test_422_bad_slug_in_json(self, client):
        r = client.post(
            "/templates",
            json={"slug": "UPPER_CASE", "yaml": _MINIMAL_YAML},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# PUT /templates/{slug} — update
# ---------------------------------------------------------------------------

class TestUpdateTemplate:
    def test_update_existing_template(self, client, template_home):
        kt.save_template("update-me", _MINIMAL_YAML)
        updated = _MINIMAL_YAML.replace("Test Template", "Updated Template")
        r = client.put(
            "/templates/update-me",
            content=updated,
            headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 200
        assert r.json()["template"]["name"] == "Updated Template"

    def test_404_for_nonexistent_template(self, client):
        r = client.put(
            "/templates/ghost",
            content=_MINIMAL_YAML,
            headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 404

    def test_413_oversized_body(self, client, template_home):
        kt.save_template("update-me", _MINIMAL_YAML)
        big = "x" * (kt.MAX_TEMPLATE_BYTES + 1)
        r = client.put(
            "/templates/update-me",
            content=big,
            headers={"Content-Type": "text/yaml"},
        )
        assert r.status_code == 413


# ---------------------------------------------------------------------------
# DELETE /templates/{slug} — delete
# ---------------------------------------------------------------------------

class TestDeleteTemplate:
    def test_delete_existing(self, client, template_home):
        kt.save_template("del-me", _MINIMAL_YAML)
        r = client.delete("/templates/del-me")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_404_for_missing(self, client):
        r = client.delete("/templates/no-such")
        assert r.status_code == 404

    def test_422_for_bad_slug(self, client):
        r = client.delete("/templates/UPPER!")
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /templates/{slug}/instantiate
# ---------------------------------------------------------------------------

class TestInstantiateTemplate:
    def test_happy_path_no_body(self, client, template_home):
        kt.save_template("board-tmpl", _YAML_WITH_BOARD)
        r = client.post("/templates/board-tmpl/instantiate")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["created"] == 2
        assert "board_slug" in body
        assert "instance_id" in body

    def test_happy_path_with_json_body(self, client, template_home):
        kt.save_template("board-tmpl", _YAML_WITH_BOARD)
        r = client.post(
            "/templates/board-tmpl/instantiate",
            json={"board_slug": "custom-board", "auto_dispatch": False},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["board_slug"] == "custom-board"

    def test_404_for_missing_template(self, client):
        r = client.post("/templates/ghost/instantiate")
        assert r.status_code == 404

    def test_409_when_cap_exceeded(self, client, template_home, monkeypatch):
        kt.save_template("board-tmpl", _YAML_WITH_BOARD)
        # Seed 2 open tasks directly via kt.instantiate on a fixed board slug.
        kt.instantiate("board-tmpl", board_slug="cap-test-board")
        # Now patch kt.instantiate to enforce _cap=1 so the REST route gets 409.
        real_instantiate = kt.instantiate

        def capped_instantiate(*args, **kwargs):
            kwargs["_cap"] = 1  # 2 existing open tasks > 1 → InstantiationRefused
            return real_instantiate(*args, **kwargs)

        monkeypatch.setattr(kt, "instantiate", capped_instantiate)
        r = client.post(
            "/templates/board-tmpl/instantiate",
            json={"board_slug": "cap-test-board"},
        )
        assert r.status_code == 409

    def test_413_oversized_instantiate_body(self, client, template_home):
        kt.save_template("board-tmpl", _YAML_WITH_BOARD)
        big = "x" * (kt.MAX_TEMPLATE_BYTES + 1)
        r = client.post(
            "/templates/board-tmpl/instantiate",
            content=big,
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 413

    def test_422_bad_json_body(self, client, template_home):
        kt.save_template("board-tmpl", _YAML_WITH_BOARD)
        r = client.post(
            "/templates/board-tmpl/instantiate",
            content=b"{not valid json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 422
