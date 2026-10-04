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


def _seed_state(state_path: Path):
    """listable l1,l2 (+ unbound l3); child c1; orphaned delegate d1; archived a1."""
    from hermes_state import SessionDB
    db = SessionDB(db_path=state_path)
    try:
        for sid in ("l1", "l2", "l3", "a1", "d1"):
            db.create_session(sid, source="cli")
        db.create_session("c1", source="cli", parent_session_id="l1")
        db.set_session_archived("a1", True)
        db._execute_write(lambda c: c.execute(
            "UPDATE sessions SET model_config = ? WHERE id = 'd1'", ('{"_delegate_from": "__orphaned__"}',)))
    finally:
        db.close()


def test_listable_counts_unfiled_and_etag(client, home):
    _seed_state(home / "state.db")
    with projects_db.connect_closing(db_path=home / "projects.db") as conn:
        a = projects_db.create_project(conn, name="Alpha")
        b = projects_db.create_project(conn, name="Beta")
        for sid in ("l1", "c1", "d1", "a1", "gone"):
            projects_db.bind_session(conn, a, sid)
        projects_db.bind_session(conn, b, "l2")
    r = client.get("/project-map")
    body = r.json()
    assert body["counts"] == {a: 1, b: 1}
    assert body["listable_total"] == 3 and body["unfiled"] == 1
    with projects_db.connect_closing(db_path=home / "projects.db") as conn:
        projects_db.bind_session(conn, b, "l3")
    r2 = client.get("/project-map", headers={"If-None-Match": r.headers["etag"]})
    assert r2.status_code == 200 and r2.json()["unfiled"] == 0


def test_counts_without_state_db_zero_and_no_file(client, home):
    _seed(home / "projects.db")
    body = client.get("/project-map").json()
    assert body["counts"] == {} and body["listable_total"] == 0 and body["unfiled"] == 0
    assert not (home / "state.db").exists()


def _chain(state_path: Path, ids, *, archived=()):
    """ids[0] root -> ... -> ids[-1] tip, each parent ended by compression."""
    from hermes_state import SessionDB
    db = SessionDB(db_path=state_path)
    try:
        parent = None
        for sid in ids:
            db.create_session(sid, source="cli", parent_session_id=parent)
            parent = sid
        def _w(c):
            for sid in ids[:-1]:
                c.execute("UPDATE sessions SET end_reason = 'compression', ended_at = 1 WHERE id = ?", (sid,))
            for sid in archived:
                c.execute("UPDATE sessions SET archived = 1 WHERE id = ?", (sid,))
        db._execute_write(_w)
    finally:
        db.close()


def _bind(home, pairs):
    with projects_db.connect_closing(db_path=home / "projects.db") as conn:
        ids = {}
        for name, sid in pairs:
            if name not in ids:
                ids[name] = projects_db.create_project(conn, name=name)
            projects_db.bind_session(conn, ids[name], sid)
    return ids


def test_tip_inherits_nearest_bound_ancestor_multi_level(client, home):
    _chain(home / "state.db", ["r", "m1", "m2", "tip"])
    ids = _bind(home, [("Root", "r"), ("Mid", "m1")])
    body = client.get("/project-map").json()
    assert body["sessions"]["tip"] == ids["Mid"] and body["sessions"]["m2"] == ids["Mid"]
    assert body["sessions"]["r"] == ids["Root"] and body["sessions"]["m1"] == ids["Mid"]
    assert body["inherited"] == {"tip": True, "m2": True}
    # one listable row (root r, shown as tip) filed under the tip's effective project
    assert body["counts"] == {ids["Mid"]: 1} and body["listable_total"] == 1 and body["unfiled"] == 0


def test_explicit_binding_on_tip_wins(client, home):
    _chain(home / "state.db", ["r", "tip"])
    ids = _bind(home, [("Root", "r"), ("Tip", "tip")])
    body = client.get("/project-map").json()
    assert body["sessions"]["tip"] == ids["Tip"] and body["inherited"] == {}
    assert body["counts"] == {ids["Tip"]: 1}


def test_archived_ancestor_still_inherited(client, home):
    _chain(home / "state.db", ["r", "tip"], archived=("r",))
    ids = _bind(home, [("Root", "r")])
    body = client.get("/project-map").json()
    assert body["sessions"]["tip"] == ids["Root"] and body["inherited"] == {"tip": True}


def test_parent_cycle_terminates(client, home):
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session("x", source="cli")
        db.create_session("y", source="cli", parent_session_id="x")
        db._execute_write(lambda c: c.execute("UPDATE sessions SET parent_session_id = 'y' WHERE id = 'x'"))
    finally:
        db.close()
    _bind(home, [("Other", "unrelated")])
    r = client.get("/project-map")
    assert r.status_code == 200 and r.json()["inherited"] == {}
