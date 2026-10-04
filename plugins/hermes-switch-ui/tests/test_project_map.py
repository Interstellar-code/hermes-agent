"""test_project_map.py — GET /project-map on hermes-switch-ui dashboard/plugin_api.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_API_PATH = _PLUGIN_DIR / "dashboard" / "plugin_api.py"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

projects_db = pytest.importorskip("hermes_cli.projects_db")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("SWITCHUI_STATE_PATH", str(tmp_path / "state.json"))
    return h


@pytest.fixture
def client(home, monkeypatch):
    spec = importlib.util.spec_from_file_location("plugin_api_project_map_test", _API_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router)
    app.dependency_overrides[mod._require_auth] = lambda: None
    return TestClient(app)


def _seed(db_path: Path):
    with projects_db.connect_closing(db_path=db_path) as conn:
        a = projects_db.create_project(conn, name="Alpha")
        b = projects_db.create_project(conn, name="beta")
        projects_db.archive_project(conn, b)
        projects_db.bind_session(conn, a, "s1")
        projects_db.bind_session(conn, b, "s2")
    return a, b


def test_no_db_returns_empty_and_creates_nothing(client, home):
    r = client.get("/project-map")
    assert r.status_code == 200
    body = r.json()
    assert body["projects"] == [] and body["sessions"] == {}
    assert not (home / "projects.db").exists()
    assert r.headers["cache-control"] == "no-cache"


def test_shape_archived_bool_and_bindings(client, home):
    a, b = _seed(home / "projects.db")
    body = client.get("/project-map?profile=current").json()
    assert [p["id"] for p in body["projects"]] == [a, b]  # archived sorts last
    assert set(body["projects"][0]) == {"id", "slug", "name", "icon", "color", "archived", "board_slug"}
    assert body["projects"][0]["archived"] is False and body["projects"][1]["archived"] is True
    assert body["sessions"] == {"s1": a, "s2": b}


def test_default_profile_reads_root_home(client, home):
    a, _ = _seed(home / "projects.db")
    body = client.get("/project-map?profile=Default").json()
    assert body["sessions"]["s1"] == a


def test_named_profile(client, home):
    pdir = home / "profiles" / "work"
    pdir.mkdir(parents=True)
    a, _ = _seed(pdir / "projects.db")
    body = client.get("/project-map?profile=Work").json()
    assert body["sessions"]["s1"] == a


def test_etag_stable_304_and_changes_after_bind(client, home):
    a, _ = _seed(home / "projects.db")
    r1 = client.get("/project-map")
    etag = r1.headers["etag"]
    assert client.get("/project-map").headers["etag"] == etag
    r304 = client.get("/project-map", headers={"If-None-Match": etag})
    assert r304.status_code == 304 and r304.content == b""
    with projects_db.connect_closing(db_path=home / "projects.db") as conn:
        projects_db.bind_session(conn, a, "s3")
    r2 = client.get("/project-map", headers={"If-None-Match": etag})
    assert r2.status_code == 200 and r2.headers["etag"] != etag


def test_unknown_profile_404(client):
    assert client.get("/project-map?profile=nope").status_code == 404


def test_bad_profile_name_400(client):
    assert client.get("/project-map?profile=../etc").status_code == 400
