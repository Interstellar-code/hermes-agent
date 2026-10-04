"""GET /runs limit/status pushdown, GET /health scheduler fields, heartbeat, llm bridge."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.wiring import create_engine
from engine.runtime.scheduler_tick import (
    heartbeat_path, read_heartbeat, run_scheduler_tick_loop, write_heartbeat,
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "hermes-switch"
    engine = create_engine(db_path=str(home / "switchui-workflows.db"), seed_bundled=False, write_manifest=False, crash_recovery=False)
    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    monkeypatch.setattr(api_mod, "_engine", lambda: engine)
    app = FastAPI()
    app.include_router(api_mod.router)
    with TestClient(app) as c:
        yield c, engine, home


_YAML = "id: wf\nname: WF\nnodes:\n  - id: a\n    prompt: hi\n"


def _seed(engine, n, status_for):
    ids = []
    if not engine._conn.execute("SELECT 1 FROM workflow_definitions WHERE id='wf'").fetchone():
        engine._conn.execute(
            "INSERT INTO workflow_definitions (id, name, yaml, source, checksum, created_at, updated_at) VALUES ('wf','WF',?,'user','x',0,0)", (_YAML,))
    for i in range(n):
        r = engine._run_store.create_workflow_run(
            workflow_id="wf", conversation_id=f"c{i}", working_path="/", user_message="m",
        )
        engine._run_store.update_workflow_run(r["id"], status=status_for(i))
        ids.append(r["id"])
    return ids


def test_runs_limit_default_clamp_and_order(env):
    c, engine, _ = env
    _seed(engine, 3, lambda i: "completed")
    assert len(c.get("/runs").json()["runs"]) == 3
    assert len(c.get("/runs?limit=2").json()["runs"]) == 2
    assert len(c.get("/runs?limit=0").json()["runs"]) == 1  # clamped to 1
    assert len(c.get("/runs?limit=abc").json()["runs"]) == 3  # falls back to default
    started = [r["started_at"] for r in c.get("/runs").json()["runs"]]
    assert started == sorted(started, reverse=True)


def test_runs_status_filter_in_sql_before_limit(env):
    c, engine, _ = env
    _seed(engine, 5, lambda i: "failed" if i == 0 else "completed")
    # oldest run is the only failed one: must survive limit=1 (filter applied before LIMIT)
    runs = c.get("/runs?status=failed&limit=1").json()["runs"]
    assert [r["status"] for r in runs] == ["failed"]
    both = c.get("/runs?status=failed,completed").json()["runs"]
    assert len(both) == 5


def test_health_scheduler_fields(env):
    c, _, home = env
    body = c.get("/health").json()
    assert body["ok"] and body["profile"] == "hermes-switch"
    assert body["scheduler_alive"] is False and body["scheduler_heartbeat_at"] is None
    write_heartbeat(heartbeat_path(home), 10.0)
    body = c.get("/health").json()
    assert body["scheduler_alive"] is True and body["scheduler_heartbeat_at"] > 0


def test_heartbeat_goes_stale_after_3_intervals(tmp_path):
    p = heartbeat_path(tmp_path)
    p.write_text(json.dumps({"at": int(time.time() * 1000) - 31_000, "interval_s": 10.0}))
    assert read_heartbeat(p)[0] is False
    p.write_text(json.dumps({"at": int(time.time() * 1000) - 29_000, "interval_s": 10.0}))
    assert read_heartbeat(p)[0] is True
    p.write_text("garbage")
    assert read_heartbeat(p) == (False, None)


def test_tick_loop_writes_heartbeat(tmp_path):
    class Eng:
        async def fire_due_scheduled_runs(self):
            return 0

    hb = heartbeat_path(tmp_path)

    async def go():
        t = asyncio.create_task(run_scheduler_tick_loop(Eng(), 0.05, hb))
        await asyncio.sleep(0.15)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)

    asyncio.run(go())
    assert read_heartbeat(hb)[0] is True


def test_host_llm_visible_across_module_copies(monkeypatch):
    """Dashboard loads a flat `_shared`; set_llm via the package copy must still reach it."""
    import engine as engine_pkg
    import _shared
    sentinel = object()
    monkeypatch.setattr(_shared, "_engines", {})
    monkeypatch.setattr(_shared, "_llm", None)
    monkeypatch.setattr(engine_pkg, "HOST_LLM", sentinel, raising=False)
    monkeypatch.setattr(_shared, "_home_key", lambda: ":memory:")
    monkeypatch.setenv("WORKFLOW_DB_PATH", ":memory:")
    assert _shared.get_engine()._runner._llm is sentinel


def test_runs_status_filter_strips_and_validates(env):
    c, engine, _ = env
    _seed(engine, 2, lambda i: "failed" if i == 0 else "running")
    assert len(c.get("/runs?status=running, failed").json()["runs"]) == 2
    assert len(c.get("/runs?status=failed,bogus").json()["runs"]) == 1
    assert c.get("/runs?status=bogus, ").status_code == 400


def test_dashboard_flat_copy_health_without_home_patch(tmp_path, monkeypatch):
    """web_server loads plugin_api flat; heartbeat/profile must follow WORKFLOW_DB_PATH."""
    import importlib.util
    from pathlib import Path
    home = tmp_path / "profiles" / "hs"
    monkeypatch.setenv("WORKFLOW_DB_PATH", str(home / "switchui-workflows.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import _shared
    monkeypatch.setattr(_shared, "_engines", {})
    path = Path(__file__).resolve().parent.parent / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("flat_plugin_api", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router)
    with TestClient(app) as c:
        assert c.get("/health").json()["scheduler_alive"] is False
        write_heartbeat(heartbeat_path(home), 10.0)
        body = c.get("/health").json()
    assert body["profile"] == "hs" and body["scheduler_alive"] is True


def test_runner_reads_host_llm_lazily(monkeypatch):
    """Engine built before register() still gets the llm set later on the package."""
    import engine as engine_pkg
    eng = create_engine(db_path=":memory:", seed_bundled=False, write_manifest=False, crash_recovery=False)
    sentinel = object()
    monkeypatch.setattr(engine_pkg, "HOST_LLM", sentinel, raising=False)
    assert eng._runner._build_ctx("r", "/tmp").llm is sentinel
