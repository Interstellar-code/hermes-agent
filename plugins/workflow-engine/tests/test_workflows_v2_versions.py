"""Definition version history (B2): a snapshot on every save, migration 012,
the versions routes and count retention."""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import engine.store.definition_store as ds
from engine.db.client import open_db
from engine.db.migrate import ensure_schema
from engine.store.definition_store import DefinitionStore, _sha256
from engine.store.run_store import STORE_LOCK, RunStore
from engine.wiring import create_engine


def _yaml(tag: str, nodes: int = 1) -> str:
    body = "".join(f"  - id: n{i}\n    bash: echo {tag}\n" for i in range(nodes))
    return f"name: wf\ndescription: {tag}\nnodes:\n{body}"


GATED = "name: wf\ndescription: d\nnodes:\n  - id: gate\n    approval:\n      message: ok?\n"


def _arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def eng():
    e = create_engine(db_path=":memory:", seed_bundled=False, write_manifest=False, crash_recovery=False)
    yield e
    _arun(e.shutdown())


@pytest.fixture()
def client(eng):
    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    original = api_mod._engine
    api_mod._engine = lambda: eng
    app = FastAPI()
    app.include_router(api_mod.router)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    api_mod._engine = original


def _sql(eng, sql, args=()):
    with STORE_LOCK:
        rows = eng._conn.execute(sql, args).fetchall()
        eng._conn.commit()
    return rows


def _snaps(eng, wf="wf"):
    return {r["checksum"]: dict(r) for r in _sql(
        eng, "SELECT * FROM workflow_definition_snapshots WHERE workflow_id = ?", (wf,))}


def _post(client, yaml_text, def_id="wf", **extra):
    return client.post("/definitions", json={"id": def_id, "name": "wf", "yaml": yaml_text,
                                             "source": "user", **extra})


# ── a snapshot per save path ────────────────────────────────────────────────

def test_upsert_and_import_snapshot(eng, client):
    assert _post(client, _yaml("a")).status_code == 201
    assert _post(client, _yaml("b"), save_source="import").status_code == 201
    snaps = _snaps(eng)
    assert snaps[_sha256(_yaml("a"))]["source"] == "save"
    assert snaps[_sha256(_yaml("b"))]["source"] == "import"
    assert all(s["saved_at"] for s in snaps.values())
    assert _post(client, _yaml("c"), save_source="bogus").status_code == 400


def test_seed_edit_and_reset_snapshot(eng, tmp_path):
    factory = _yaml("factory")
    (tmp_path / "wf.yaml").write_text(factory, encoding="utf-8")
    store = eng._def_store
    assert store.seed_bundled(tmp_path)["inserted"] == 1
    assert _snaps(eng)[_sha256(factory)]["source"] == "seed"

    store.mark_user_edit("wf", _yaml("edited"))  # POST on a bundled row
    assert _snaps(eng)[_sha256(_yaml("edited"))]["source"] == "save"

    store.reset_to_factory("wf", factory)
    snaps = _snaps(eng)
    assert len(snaps) == 2 and snaps[_sha256(factory)]["source"] == "reset"

    # factory upgraded upstream -> seed UPDATE snapshots the new content
    store.reset_to_factory("wf", factory)
    (tmp_path / "wf.yaml").write_text(_yaml("factory-v2"), encoding="utf-8")
    assert store.seed_bundled(tmp_path)["updated"] == 1
    assert _snaps(eng)[_sha256(_yaml("factory-v2"))]["source"] == "seed"
    # unchanged factory -> no new snapshot
    assert store.seed_bundled(tmp_path)["skipped"] == 1
    assert len(_snaps(eng)) == 3


def test_identical_resave_adds_no_row(eng, client):
    for _ in range(3):
        _post(client, _yaml("a"))
    _post(client, _yaml("a"), expected_checksum=_sha256(_yaml("a")))  # forced CAS write
    assert len(_snaps(eng)) == 1


def test_run_and_save_of_same_yaml_share_one_row(eng, client):
    _post(client, GATED)
    run = _arun(eng.start_run("wf", {}, {"kind": "manual", "conversation_id": "c"}))
    snaps = _snaps(eng)
    assert list(snaps) == [run["definition_checksum"]] == [_sha256(GATED)]
    assert snaps[_sha256(GATED)]["source"] == "save"  # a run re-pin keeps the label
    assert client.get("/definitions/wf/versions").json()[0]["in_use_by_runs"] == 1


def test_run_start_snapshot_labelled_run(eng):
    # definition written by something other than a save path (pre-B2 row)
    _sql(eng, "INSERT INTO workflow_definitions (id, name, source, yaml, checksum, created_at, updated_at) "
              "VALUES ('wf', 'wf', 'user', ?, ?, 1, 1)", (GATED, _sha256(GATED)))
    _arun(eng.start_run("wf", {}, {"kind": "manual", "conversation_id": "c"}))
    (snap,) = _snaps(eng).values()
    assert snap["source"] == "run" and snap["saved_at"] == snap["created_at"]


# ── routes ──────────────────────────────────────────────────────────────────

def test_versions_list_order_and_fields(eng, client):
    _post(client, _yaml("a", nodes=1))
    _post(client, _yaml("b", nodes=3))
    _sql(eng, "UPDATE workflow_definition_snapshots SET saved_at = 1 WHERE checksum = ?",
         (_sha256(_yaml("a")),))
    body = client.get("/definitions/wf/versions").json()
    assert [v["checksum"] for v in body] == [_sha256(_yaml("b", nodes=3)), _sha256(_yaml("a"))]
    assert set(body[0]) == {"checksum", "version", "saved_at", "source", "node_count",
                            "size_bytes", "in_use_by_runs"}
    assert body[0]["node_count"] == 3 and body[1]["node_count"] == 1
    assert body[0]["size_bytes"] == len(_yaml("b", nodes=3).encode())
    assert body[0]["in_use_by_runs"] == 0 and body[1]["saved_at"] == 1


def test_get_version_and_404s(eng, client):
    _post(client, _yaml("a", nodes=2))
    ck = _sha256(_yaml("a", nodes=2))
    body = client.get(f"/definitions/wf/versions/{ck}").json()
    assert body["yaml"] == _yaml("a", nodes=2) and body["source"] == "save"
    assert body["parsed"] == client.get("/definitions/wf/parsed").json()["parsed"]
    assert client.get("/definitions/nope/versions").status_code == 404
    assert client.get(f"/definitions/nope/versions/{ck}").status_code == 404
    assert client.get("/definitions/wf/versions/deadbeef").status_code == 404


def test_unparsable_snapshot_does_not_500(eng, client):
    _post(client, _yaml("a"))
    _sql(eng, "INSERT INTO workflow_definition_snapshots (workflow_id, checksum, yaml, created_at, "
              "saved_at, source) VALUES ('wf', 'bad', ':: [not yaml', 1, 1, 'run')")
    rows = {v["checksum"]: v for v in client.get("/definitions/wf/versions").json()}
    assert rows["bad"]["node_count"] is None
    r = client.get("/definitions/wf/versions/bad")
    assert r.status_code == 200 and "error" in r.json()["parsed"]


def test_route_order(eng, client):
    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    paths = [r.path for r in api_mod.router.routes if "GET" in getattr(r, "methods", ())]
    assert paths.index("/definitions/{def_id}/versions") < paths.index("/definitions/{def_id}")
    assert paths.index("/definitions/{def_id}/versions/{checksum}") < paths.index("/definitions/{def_id}")
    _post(client, _yaml("a"))
    assert isinstance(client.get("/definitions/wf/versions").json(), list)
    _post(client, _yaml("v"), def_id="versions")  # a workflow literally named "versions"
    assert client.get("/definitions/versions").json()["definition"]["id"] == "versions"
    assert "definition_versions" in client.get("/health").json()["features"]


# ── retention ───────────────────────────────────────────────────────────────

def _save_aged(eng, tags):
    """Save each tag (default SNAPSHOT_KEEP, so nothing is pruned yet), then
    age it: saved_at = position, created_at past the grace window."""
    for i, tag in enumerate(tags):
        _arun(eng.upsert_definition("wf", _yaml(tag)))
        _sql(eng, "UPDATE workflow_definition_snapshots SET saved_at = ?, created_at = 1 "
                  "WHERE checksum = ?", (i + 1, _sha256(_yaml(tag))))


def test_retention_keeps_newest_n(eng, monkeypatch):
    _save_aged(eng, ["y0", "y1", "y2", "y3", "y4", "y5"])
    monkeypatch.setattr(ds, "SNAPSHOT_KEEP", 3)
    _arun(eng.upsert_definition("wf", _yaml("y6")))
    assert set(_snaps(eng)) == {_sha256(_yaml(t)) for t in ("y4", "y5", "y6")}


def test_retention_never_prunes_run_referenced(eng, monkeypatch):
    _save_aged(eng, ["y0", "y1", "y2", "y3", "y4", "y5"])
    monkeypatch.setattr(ds, "SNAPSHOT_KEEP", 3)
    store = eng._run_store
    # oldest two referenced: y0 as a run's definition, y1 as another run's subgraph pin
    store.create_workflow_run(workflow_id="wf", conversation_id="c", working_path="/tmp",
                              user_message="m", definition_checksum=_sha256(_yaml("y0")))
    other = store.create_workflow_run(workflow_id="wf", conversation_id="c", working_path="/tmp",
                                      user_message="m")
    store.update_workflow_run(other["id"], metadata={"subgraph_pins": {"wf": _sha256(_yaml("y1"))}})
    _arun(eng.upsert_definition("wf", _yaml("y6")))
    assert set(_snaps(eng)) == {_sha256(_yaml(t)) for t in ("y0", "y1", "y4", "y5", "y6")}


def test_referenced_rows_do_not_use_up_slots(eng, monkeypatch):
    # The newest rows are run-referenced: the N unreferenced ones below them stay.
    _save_aged(eng, ["y0", "y1", "y2", "y3", "y4", "y5"])
    monkeypatch.setattr(ds, "SNAPSHOT_KEEP", 3)
    for tag in ("y4", "y5"):
        eng._run_store.create_workflow_run(workflow_id="wf", conversation_id="c", working_path="/tmp",
                                           user_message="m", definition_checksum=_sha256(_yaml(tag)))
    _arun(eng.upsert_definition("wf", _yaml("y6")))
    assert set(_snaps(eng)) == {_sha256(_yaml(t)) for t in ("y2", "y3", "y4", "y5", "y6")}


def test_retention_grace_keeps_recent_pins(eng, monkeypatch):
    _save_aged(eng, ["y0", "y1"])
    monkeypatch.setattr(ds, "SNAPSHOT_KEEP", 1)
    _sql(eng, "UPDATE workflow_definition_snapshots SET created_at = ? WHERE checksum = ?",
         (ds._now_ms(), _sha256(_yaml("y0"))))  # just re-pinned by a starting run
    _arun(eng.upsert_definition("wf", _yaml("y2")))
    assert set(_snaps(eng)) == {_sha256(_yaml(t)) for t in ("y0", "y2")}


def test_age_sweep_keeps_save_snapshots(eng):
    store = eng._def_store
    store.upsert_definition(definition_id="wf", yaml_text=_yaml("a"))
    eng._run_store.insert_definition_snapshot(workflow_id="wf", checksum="run-only", version=None, yaml="x")
    _sql(eng, "UPDATE workflow_definition_snapshots SET created_at = 1")
    eng._run_store.delete_terminal_runs_older_than(1)
    assert set(_snaps(eng)) == {_sha256(_yaml("a"))}


# ── migration 012 ───────────────────────────────────────────────────────────

def _version(conn) -> int:
    return int(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0])


def _cols(conn) -> set:
    return {r["name"] for r in conn.execute("PRAGMA table_info(workflow_definition_snapshots)")}


def _to_011(conn):
    conn.execute("ALTER TABLE workflow_definition_snapshots DROP COLUMN saved_at")
    conn.execute("ALTER TABLE workflow_definition_snapshots DROP COLUMN source")
    conn.execute("UPDATE schema_meta SET value='11' WHERE key='schema_version'")
    conn.commit()


def test_migration_012_fresh_db():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        assert _version(conn) >= 12
        assert {"saved_at", "source"} <= _cols(conn)


def test_migration_012_on_011_db_backfills():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        _to_011(conn)
        conn.executemany(
            "INSERT INTO workflow_definition_snapshots (workflow_id, checksum, version, yaml, created_at) "
            "VALUES (?, ?, NULL, 'x', ?)", [("wf", "a", 111), ("wf", "b", 222), ("other", "a", 333)])
        conn.commit()
        ensure_schema(conn)
        assert _version(conn) >= 12
        rows = {(r["workflow_id"], r["checksum"]): (r["saved_at"], r["source"]) for r in
                conn.execute("SELECT * FROM workflow_definition_snapshots")}
        assert rows == {("wf", "a"): (111, "run"), ("wf", "b"): (222, "run"), ("other", "a"): (333, "run")}


def test_migration_012_twice_is_noop():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        _to_011(conn)
        conn.execute("INSERT INTO workflow_definition_snapshots (workflow_id, checksum, yaml, created_at) "
                     "VALUES ('wf', 'a', 'x', 5)")
        conn.commit()
        ensure_schema(conn)
        before = (_version(conn), [tuple(r) for r in conn.execute("SELECT * FROM workflow_definition_snapshots")])
        ensure_schema(conn)
        assert (_version(conn), [tuple(r) for r in conn.execute(
            "SELECT * FROM workflow_definition_snapshots")]) == before
        # re-applying 012 itself (columns already there) converges too
        conn.execute("UPDATE schema_meta SET value='11' WHERE key='schema_version'")
        conn.commit()
        ensure_schema(conn)
        assert (_version(conn), [tuple(r) for r in conn.execute(
            "SELECT * FROM workflow_definition_snapshots")]) == before


def test_store_save_snapshots_without_engine():
    """DefinitionStore on a bare migrated connection snapshots too (no engine)."""
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        DefinitionStore(conn).upsert_definition(definition_id="wf", yaml_text=_yaml("a"))
        assert RunStore(conn).get_definition_snapshot("wf", _sha256(_yaml("a")))["source"] == "save"


# ── review follow-ups ───────────────────────────────────────────────────────

def test_delete_removes_history_so_recreated_id_starts_clean(eng, client):
    _post(client, _yaml("old"))
    assert client.delete("/definitions/wf").status_code == 200
    assert _snaps(eng) == {}
    _post(client, _yaml("new"))
    assert [v["checksum"] for v in client.get("/definitions/wf/versions").json()] == [_sha256(_yaml("new"))]


def test_delete_keeps_snapshot_pinned_as_subgraph_elsewhere(eng, client):
    _post(client, _yaml("a"))
    _post(client, _yaml("b"))
    _post(client, GATED, def_id="parent")
    run = eng._run_store.create_workflow_run(workflow_id="parent", conversation_id="c",
                                             working_path="/tmp", user_message="m")
    eng._run_store.update_workflow_run(run["id"], metadata={"subgraph_pins": {"wf": _sha256(_yaml("a"))}})
    assert client.delete("/definitions/wf").status_code == 200
    assert set(_snaps(eng)) == {_sha256(_yaml("a"))}


def test_in_use_counts_subgraph_pins(eng, client):
    _post(client, _yaml("a"))
    _post(client, GATED, def_id="other")
    store = eng._run_store
    store.create_workflow_run(workflow_id="wf", conversation_id="c", working_path="/tmp",
                              user_message="m", definition_checksum=_sha256(_yaml("a")))
    other = store.create_workflow_run(workflow_id="other", conversation_id="c", working_path="/tmp",
                                      user_message="m")
    store.update_workflow_run(other["id"], metadata={"subgraph_pins": {"wf": _sha256(_yaml("a"))}})
    assert client.get("/definitions/wf/versions").json()[0]["in_use_by_runs"] == 2


def test_seed_snapshot_failure_rolls_back_that_file_only(eng, tmp_path, monkeypatch):
    (tmp_path / "a.yaml").write_text(_yaml("a"), encoding="utf-8")
    (tmp_path / "b.yaml").write_text(_yaml("b"), encoding="utf-8")
    store = eng._def_store
    real = DefinitionStore._snapshot

    def flaky(self, definition_id, source):
        if definition_id == "a":
            raise RuntimeError("boom")
        return real(self, definition_id, source)

    monkeypatch.setattr(DefinitionStore, "_snapshot", flaky)
    result = store.seed_bundled(tmp_path)
    assert result["errors"] == 1 and result["inserted"] == 1
    assert store.get_definition("a") is None
    assert store.get_definition("b") is not None
    assert list(_snaps(eng, "b")) == [_sha256(_yaml("b"))] and _snaps(eng, "a") == {}


def test_null_save_source_is_default(eng, client):
    assert _post(client, _yaml("a"), save_source=None).status_code == 201
    assert _snaps(eng)[_sha256(_yaml("a"))]["source"] == "save"
    assert _post(client, _yaml("b"), save_source="").status_code == 400
