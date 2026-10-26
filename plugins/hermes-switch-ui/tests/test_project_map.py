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


def _listable_counts(body):
    out = {}
    for sid in body["listable"]:
        pid = body["sessions"][sid]
        out[pid] = out.get(pid, 0) + 1
    return out


def test_listable_ids_are_exactly_the_counted_rows(client, home):
    _seed_state(home / "state.db")
    with projects_db.connect_closing(db_path=home / "projects.db") as conn:
        a = projects_db.create_project(conn, name="Alpha")
        for sid in ("l1", "c1", "d1", "a1", "gone"):
            projects_db.bind_session(conn, a, sid)
    body = client.get("/project-map").json()
    # Child, delegate, archived and unknown bindings stay in `sessions` but are not listable.
    assert body["listable"] == ["l1"]
    assert _listable_counts(body) == body["counts"]


def test_listable_names_the_compression_tip_not_bound_segments(client, home):
    _chain(home / "state.db", ["r", "m1", "m2", "tip"])
    _sessions(home / "state.db", [("solo", None), ("b1", "solo")], config={"b1": BR})
    ids = _bind(home, [("Root", "r"), ("Mid", "m1"), ("Solo", "solo")])
    body = client.get("/project-map").json()
    assert body["listable"] == ["b1", "solo", "tip"]
    assert _listable_counts(body) == body["counts"] == {ids["Mid"]: 1, ids["Solo"]: 2}


def test_counts_without_state_db_zero_and_no_file(client, home):
    _seed(home / "projects.db")
    body = client.get("/project-map").json()
    assert body["counts"] == {} and body["listable_total"] == 0 and body["unfiled"] == 0
    assert not (home / "state.db").exists()


def _sessions(state_path: Path, rows, *, compressed=(), archived=(), config=None):
    """rows: [(id, parent)]; ``compressed`` parents end by compression; ``config``: {id: model_config}."""
    import json as _json
    from hermes_state import SessionDB
    db = SessionDB(db_path=state_path)
    try:
        for sid, parent in rows:
            db.create_session(sid, source="cli", parent_session_id=parent)
        def _w(c):
            for sid in compressed:
                c.execute("UPDATE sessions SET end_reason = 'compression', ended_at = 1 WHERE id = ?", (sid,))
            for sid in archived:
                c.execute("UPDATE sessions SET archived = 1 WHERE id = ?", (sid,))
            for sid, cfg in (config or {}).items():
                c.execute("UPDATE sessions SET model_config = ? WHERE id = ?", (_json.dumps(cfg), sid))
        db._execute_write(_w)
    finally:
        db.close()


def _chain(state_path: Path, ids, **kw):
    """ids[0] root -> ... -> ids[-1] tip, each parent ended by compression."""
    _sessions(state_path, list(zip(ids, [None, *ids[:-1]])), compressed=ids[:-1], **kw)


def _bind(home, pairs):
    with projects_db.connect_closing(db_path=home / "projects.db") as conn:
        ids = {}
        for name, sid in pairs:
            if name not in ids:
                ids[name] = projects_db.create_project(conn, name=name)
            projects_db.bind_session(conn, ids[name], sid)
    return ids


BR = {"_branched_from": "x"}


def test_compressed_tip_is_filed_not_inherited(client, home):
    _chain(home / "state.db", ["r", "m1", "m2", "tip"])
    ids = _bind(home, [("Root", "r"), ("Mid", "m1")])
    body = client.get("/project-map").json()
    assert body["sessions"]["tip"] == ids["Mid"]
    assert body["inherited"] == {} and body["binding_owner"] == {"tip": "m1"}
    assert body["counts"] == {ids["Mid"]: 1} and body["listable_total"] == 1 and body["unfiled"] == 0


def test_branch_chain_inherits_multi_level(client, home):
    _sessions(home / "state.db", [("r", None), ("b1", "r"), ("b2", "b1")], config={"b1": BR, "b2": BR})
    ids = _bind(home, [("Root", "r")])
    body = client.get("/project-map").json()
    assert body["sessions"]["b2"] == ids["Root"] and body["sessions"]["b1"] == ids["Root"]
    assert body["inherited"] == {"b1": True, "b2": True}
    assert body["binding_owner"] == {"b1": "r", "b2": "r"}
    assert body["counts"] == {ids["Root"]: 3}


def test_branch_then_compression_is_inherited(client, home):
    _sessions(home / "state.db", [("r", None), ("br", "r"), ("brtip", "br")],
              compressed=("br",), config={"br": BR})
    ids = _bind(home, [("Root", "r")])
    body = client.get("/project-map").json()
    assert body["sessions"]["brtip"] == ids["Root"]
    assert body["inherited"] == {"brtip": True} and body["binding_owner"]["brtip"] == "r"


def test_explicit_binding_on_tip_wins(client, home):
    _chain(home / "state.db", ["r", "tip"])
    ids = _bind(home, [("Root", "r"), ("Tip", "tip")])
    body = client.get("/project-map").json()
    assert body["sessions"]["tip"] == ids["Tip"] and body["inherited"] == {} and body["binding_owner"] == {}
    assert body["counts"] == {ids["Tip"]: 1}


def test_archived_ancestor_still_inherited(client, home):
    _sessions(home / "state.db", [("r", None), ("b", "r")], archived=("r",), config={"b": BR})
    ids = _bind(home, [("Root", "r")])
    body = client.get("/project-map").json()
    assert body["sessions"]["b"] == ids["Root"] and body["inherited"] == {"b": True}
    assert body["listable_total"] == 1


def test_reset_child_does_not_inherit(client, home):
    _sessions(home / "state.db", [("r", None), ("n", "r")], config={"n": {"_reset_from": "r"}})
    ids = _bind(home, [("Root", "r")])
    body = client.get("/project-map").json()
    assert "n" not in body["sessions"] and body["inherited"] == {}
    assert body["counts"] == {ids["Root"]: 1} and body["unfiled"] == 1


def test_parent_cycle_terminates(client, home):
    _sessions(home / "state.db", [("x", None), ("y", "x"), ("tip", "y")], config={"tip": BR})
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        db._execute_write(lambda c: c.execute("UPDATE sessions SET parent_session_id = 'y' WHERE id = 'x'"))
    finally:
        db.close()
    _bind(home, [("Other", "unrelated")])
    r = client.get("/project-map")
    assert r.status_code == 200 and r.json()["inherited"] == {} and r.json()["listable_total"] == 1


def test_deleted_parent_does_not_propagate(client, home):
    import sqlite3
    _sessions(home / "state.db", [("b", None)], config={"b": BR})
    raw = sqlite3.connect(home / "state.db")  # legacy row: FK off, parent never existed
    raw.execute("PRAGMA foreign_keys = OFF")
    raw.execute("UPDATE sessions SET parent_session_id = 'gone' WHERE id = 'b'")
    raw.commit()
    raw.close()
    _bind(home, [("Root", "gone")])
    body = client.get("/project-map").json()
    assert body["inherited"] == {} and "b" not in body["binding_owner"]


def test_corrupt_state_db_yields_zeros(client, home):
    (home / "state.db").write_bytes(b"not a database" * 100)
    _bind(home, [("Root", "r")])
    r = client.get("/project-map")
    assert r.status_code == 200 and r.json()["listable_total"] == 0 and r.json()["counts"] == {}


def test_compression_fork_picks_live_child_as_tip(client, home):
    _sessions(home / "state.db", [("r", None), ("ended", "r"), ("live", "r")], compressed=("r",))
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        db._execute_write(lambda c: c.execute(
            "UPDATE sessions SET end_reason = 'user_exit', ended_at = 2 WHERE id = 'ended'"))
    finally:
        db.close()
    ids = _bind(home, [("A", "ended"), ("B", "live")])
    body = client.get("/project-map").json()
    assert body["listable_total"] == 1 and body["counts"] == {ids["B"]: 1}


def test_step_sql_shape_guard_falls_back_to_roots(client, home, monkeypatch, caplog):
    import hermes_state_compression
    monkeypatch.setattr(hermes_state_compression, "_CHAIN_STEP_SQL", "SELECT 1 WHERE parent.id = :x")
    _chain(home / "state.db", ["r", "tip"])
    _sessions(home / "state.db", [("solo", None)])
    ids = _bind(home, [("Root", "r"), ("Solo", "solo")])
    body = client.get("/project-map").json()
    # root stands in for its tip; not silently zero
    assert body["listable_total"] == 2 and body["counts"] == {ids["Root"]: 1, ids["Solo"]: 1}
    assert "shape changed" in caplog.text


def test_binding_to_missing_project_is_ignored(client, home):
    _sessions(home / "state.db", [("s", None)])
    with projects_db.connect_closing(db_path=home / "projects.db") as conn:
        projects_db.create_project(conn, name="Real")
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("INSERT INTO project_sessions (project_id, session_id, bound_at) VALUES ('ghost', 's', 1)")
        conn.commit()
    body = client.get("/project-map").json()
    assert body["counts"] == {} and body["unfiled"] == 1
