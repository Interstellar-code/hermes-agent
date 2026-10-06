"""B5: POST /definitions ``if_absent: true`` is create-only — an existing id
(any source, bundled included) is a 409 that writes nothing, atomically."""
from __future__ import annotations

import asyncio
import threading

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.store.definition_store import DefinitionExistsError
from engine.store.run_store import STORE_LOCK
from engine.wiring import create_engine


def _yaml(tag: str) -> str:
    return f"name: wf\ndescription: {tag}\nnodes:\n  - id: a\n    bash: echo {tag}\n"


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


def _row(eng, wf="wf"):
    with STORE_LOCK:
        r = eng._conn.execute("SELECT * FROM workflow_definitions WHERE id = ?", (wf,)).fetchone()
    return dict(r) if r else None


def _nsnaps(eng, wf="wf"):
    with STORE_LOCK:
        return eng._conn.execute(
            "SELECT COUNT(*) FROM workflow_definition_snapshots WHERE workflow_id = ?", (wf,)
        ).fetchone()[0]


def _post(client, yaml_text, def_id="wf", **extra):
    return client.post("/definitions", json={"id": def_id, "name": "wf", "yaml": yaml_text,
                                             "source": "user", **extra})


def test_new_id_created(eng, client):
    r = _post(client, _yaml("a"), if_absent=True)
    assert r.status_code == 201
    assert r.json()["definition"]["yaml"] == _yaml("a")
    assert _row(eng)["yaml"] == _yaml("a")
    assert _nsnaps(eng) == 1


def test_existing_user_id_409_writes_nothing(eng, client):
    assert _post(client, _yaml("a")).status_code == 201
    before, snaps = _row(eng), _nsnaps(eng)
    r = _post(client, _yaml("b"), if_absent=True)
    assert r.status_code == 409
    assert r.json() == {"error": "definition 'wf' already exists", "code": "id_taken"}
    assert _row(eng) == before
    assert _nsnaps(eng) == snaps
    # the losing INSERT is rolled back: an open transaction here would hold the
    # DB write lock and block every other process's writes
    assert not eng._conn.in_transaction


def test_existing_bundled_id_409_not_user_modified(eng, client, tmp_path):
    (tmp_path / "wf.yaml").write_text(_yaml("factory"), encoding="utf-8")
    assert eng._def_store.seed_bundled(tmp_path)["inserted"] == 1
    before, snaps = _row(eng), _nsnaps(eng)
    assert before["source"] == "bundled"
    r = _post(client, _yaml("b"), if_absent=True)
    assert r.status_code == 409 and r.json()["code"] == "id_taken"
    after = _row(eng)
    assert after == before and after["user_modified"] == 0
    assert _nsnaps(eng) == snaps


def test_concurrent_creates_one_winner(tmp_path):
    """Two engines (two connections) on one file DB. In one process STORE_LOCK
    serialises them, so this proves the loser goes through the PRIMARY KEY with
    no pre-SELECT; cross-process races were probed separately (B5 report)."""
    db = str(tmp_path / "wf.db")
    engines = [create_engine(db_path=db, seed_bundled=False, write_manifest=False,
                             crash_recovery=False) for _ in range(2)]
    try:
        for i in range(5):
            wf = f"race{i}"
            barrier = threading.Barrier(2)
            results: dict = {}

            def go(n):
                barrier.wait()
                try:
                    results[n] = _arun(engines[n].upsert_definition(
                        definition_id=wf, yaml_text=_yaml(f"t{n}"), if_absent=True))
                except Exception as exc:  # noqa: BLE001 — recorded for the assert
                    results[n] = exc

            threads = [threading.Thread(target=go, args=(n,)) for n in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            won = [n for n, v in results.items() if isinstance(v, dict)]
            lost = [n for n, v in results.items() if isinstance(v, DefinitionExistsError)]
            assert len(won) == 1 and len(lost) == 1, results
            for e in engines:  # both connections see the winner's content, one snapshot
                assert _row(e, wf)["yaml"] == _yaml(f"t{won[0]}")
                assert _nsnaps(e, wf) == 1
    finally:
        for e in engines:
            _arun(e.shutdown())


def test_default_path_unchanged(eng, client):
    assert _post(client, _yaml("a")).status_code == 201
    r = _post(client, _yaml("b"))
    assert r.status_code == 201 and _row(eng)["yaml"] == _yaml("b")
    r = _post(client, _yaml("c"), if_absent=False)
    assert r.status_code == 201 and _row(eng)["yaml"] == _yaml("c")
    assert _nsnaps(eng) == 3


def test_if_absent_false_on_bundled_still_edits_in_place(eng, client, tmp_path):
    (tmp_path / "wf.yaml").write_text(_yaml("factory"), encoding="utf-8")
    eng._def_store.seed_bundled(tmp_path)
    r = _post(client, _yaml("b"), if_absent=False)
    assert r.status_code == 200 and _row(eng)["user_modified"] == 1


def test_bad_flags_400(eng, client):
    assert _post(client, _yaml("a"), if_absent=True, expected_checksum="abc").status_code == 400
    for bad in ("true", 1, 0, None, [], {}):
        assert _post(client, _yaml("a"), if_absent=bad).status_code == 400, bad
    assert _row(eng) is None


def test_bundled_source_still_403(eng, client):
    r = client.post("/definitions", json={"id": "wf", "name": "wf", "yaml": _yaml("a"),
                                          "source": "bundled", "if_absent": True})
    assert r.status_code == 403 and _row(eng) is None


def test_invalid_yaml_still_422(eng, client):
    assert _post(client, "not: [valid", if_absent=True).status_code == 422


def test_feature_flag(client):
    assert "create_only" in client.get("/health").json()["features"]
