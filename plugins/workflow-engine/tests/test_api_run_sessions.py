"""
GET /runs/{run_id}/sessions — links a run to its owner chat session and to the
agent sessions / sub-agents / delegations of its nodes (read-only state.db).
Plus: workflow_run defaults conversation_id to the calling session.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
pytest.importorskip("fastapi")

from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.wiring import create_engine

_YAML = "id: wf\nname: wf\ndescription: d\nnodes:\n  - id: a\n    prompt: hi\n"

_SESSIONS_DDL = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, parent_session_id TEXT,
  started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  estimated_cost_usd REAL, actual_cost_usd REAL, title TEXT, last_activity_description TEXT
);
CREATE TABLE async_delegations (
  delegation_id TEXT PRIMARY KEY, origin_session TEXT NOT NULL, parent_session_id TEXT,
  state TEXT NOT NULL, dispatched_at REAL NOT NULL, completed_at REAL,
  updated_at REAL NOT NULL, task_json TEXT
);
"""


def _state_db(home: Path, sessions, delegations=(), with_deleg_table=True) -> None:
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state.db")
    ddl = _SESSIONS_DDL if with_deleg_table else _SESSIONS_DDL.split("CREATE TABLE async_delegations")[0]
    conn.executescript(ddl)
    for s in sessions:
        cols = ",".join(s)
        conn.execute(f"INSERT INTO sessions ({cols}) VALUES ({','.join('?' * len(s))})", tuple(s.values()))
    for d in delegations:
        conn.execute(
            "INSERT INTO async_delegations VALUES (?,?,?,?,?,?,?,?)",
            (d["id"], d["parent"], d["parent"], d["state"], 1.0, None, 1.0, json.dumps({"goal": d["goal"]})),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def env():
    home = Path(os.environ["HERMES_HOME"])  # conftest: tmp dir, never ~/.hermes
    engine = create_engine(db_path=":memory:", seed_bundled=False, write_manifest=False, crash_recovery=False)
    app = FastAPI()
    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    original = api_mod._engine
    api_mod._engine = lambda: engine
    app.include_router(api_mod.router)
    with TestClient(app, raise_server_exceptions=True) as c:
        c.post("/definitions", json={"id": "wf", "name": "wf", "yaml": _YAML, "source": "user"})
        run_id = c.post("/runs", json={
            "workflow_id": "wf", "conversation_id": "c", "user_message": "go",
        }).json()["run"]["id"]
        yield c, engine, run_id, home
    api_mod._engine = original


def _add_node(engine, run_id, nid, session_id, profile=None):
    conn = engine._run_store._conn
    conn.execute(
        "INSERT INTO node_runs (id, workflow_run_id, dag_node_id, node_type, status, started_at,"
        " assigned_agent, metadata) VALUES (?,?,?,?,?,?,?,?)",
        (nid, run_id, nid, "prompt", "completed", 1, profile,
         json.dumps({"session_id": session_id, "gateway_run_id": f"gw-{nid}"})),
    )
    conn.commit()


def test_owner_children_continuation_delegations(env):
    c, engine, run_id, home = env
    _state_db(home, [
        {"id": "owner", "source": "api_server", "started_at": 1.0, "title": "Chat"},
    ])
    _state_db(home / "profiles" / "neo", [
        {"id": "s1", "source": "api_server", "started_at": 2.0, "model": "m1",
         "input_tokens": 100, "output_tokens": 10, "actual_cost_usd": 0.5, "end_reason": "compression"},
        # compression continuation of s1 → folded into s1, its subagent surfaces under s1
        {"id": "s1b", "source": "api_server", "parent_session_id": "s1", "started_at": 3.0,
         "input_tokens": 50, "output_tokens": 5, "estimated_cost_usd": 0.25},
        {"id": "sub1", "source": "subagent", "parent_session_id": "s1", "started_at": 2.5,
         "input_tokens": 7, "output_tokens": 3, "last_activity_description": "reading"},
        {"id": "sub2", "source": "subagent", "parent_session_id": "s1b", "started_at": 3.5},
        {"id": "sub1a", "source": "subagent", "parent_session_id": "sub1", "started_at": 2.7},
    ], delegations=[
        {"id": "deleg_1", "parent": "s1", "state": "completed", "goal": "do x"},
        {"id": "deleg_2", "parent": "s1b", "state": "error", "goal": "do y"},
    ])
    engine.set_owner_session(run_id, "owner")
    _add_node(engine, run_id, "n1", "s1", profile="neo")

    body = c.get(f"/runs/{run_id}/sessions").json()
    assert body["owner"]["id"] == "owner" and body["owner"]["title"] == "Chat"
    [node] = body["nodes"]
    assert node["profile"] == "neo" and node["gateway_run_id"] == "gw-n1"
    assert node["session"]["model"] == "m1" and node["session"]["cost"] == 0.5
    assert [ch["id"] for ch in node["children"]] == ["sub1", "sub2"]
    assert all(ch["kind"] == "subagent" for ch in node["children"])
    assert node["children"][0]["children"][0]["id"] == "sub1a"
    assert node["children"][0]["last_activity_description"] == "reading"
    assert {d["id"] for d in node["delegations"]} == {"deleg_1", "deleg_2"}
    assert node["delegations"][0]["goal"] in ("do x", "do y")
    t = body["totals"]
    assert t["sessions"] == 6 and t["subagents"] == 3
    assert t["tokens"] == 110 + 55 + 10
    assert t["cost_usd"] == pytest.approx(0.75)


def test_missing_db_and_session_never_500(env):
    c, engine, run_id, home = env
    engine.set_owner_session(run_id, "ghost")          # default profile: no state.db at all
    _add_node(engine, run_id, "n1", "s1", profile="nodb")
    _add_node(engine, run_id, "n2", "s2", profile="../etc")  # rejected by profile regex
    _state_db(home / "profiles" / "neo", [], with_deleg_table=False)
    _add_node(engine, run_id, "n3", "missing", profile="neo")

    r = c.get(f"/runs/{run_id}/sessions")
    assert r.status_code == 200
    body = r.json()
    assert body["owner"] is None
    assert [n["session"] for n in body["nodes"]] == [None, None, None]
    assert body["totals"] == {"sessions": 0, "subagents": 0, "tokens": 0, "cost_usd": None}


def test_unknown_run_404(env):
    c, *_ = env
    assert c.get("/runs/nope/sessions").status_code == 404


def test_session_cap(env, monkeypatch):
    c, engine, run_id, home = env
    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    monkeypatch.setattr(api_mod, "_SESSION_CAP", 3)
    _state_db(home, [{"id": "root", "source": "cli", "started_at": 1.0}] + [
        {"id": f"k{i}", "source": "subagent", "parent_session_id": "root", "started_at": 2.0 + i}
        for i in range(10)
    ])
    _add_node(engine, run_id, "n1", "root")  # no assigned_agent → run profile (default)
    body = c.get(f"/runs/{run_id}/sessions").json()
    assert body["totals"]["sessions"] == 3
    assert len(body["nodes"][0]["children"]) == 2


# --- workflow_run: conversation_id defaults to the calling session --------

@pytest.mark.asyncio
@pytest.mark.parametrize("args,session,expected", [
    ({"id": "wf"}, "sess-123", "sess-123"),
    ({"id": "wf", "conversation_id": "explicit"}, "sess-123", "explicit"),
    ({"id": "wf"}, None, None),
])
async def test_run_workflow_conversation_default(monkeypatch, args, session, expected):
    engine = MagicMock()
    engine.start_run = AsyncMock(return_value={"id": "r1", "status": "completed"})
    engine.wait_for_run = AsyncMock(return_value={"id": "r1", "status": "completed"})
    monkeypatch.setattr("plugins.workflow_engine._shared.get_engine", lambda: engine)
    from plugins.workflow_engine.tools.run_workflow import _handler_impl, _rate_buckets
    _rate_buckets.clear()

    await _handler_impl(args, session_id=session)
    trigger = engine.start_run.call_args.kwargs["trigger"]
    assert trigger.get("conversation_id") == expected
