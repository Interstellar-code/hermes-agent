"""Conductor v2 B3: node_log live output, events query, cross-process SSE tail."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import engine.core.executor_shared as xs
from engine.core.dag_executor import DagRunContext
from engine.db.migrate import ensure_schema
from engine.emitter.bus import EventBus
from engine.nodes.bash import execute_bash_node
from engine.nodes.script import execute_script_node
from engine.schemas.dag_node import validate_dag_node
from engine.store.run_store import RunStore
from engine.wiring import create_engine


def _arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _node(data):
    node, errors = validate_dag_node(data, 0)
    assert not errors, errors
    return node


def _ctx():
    events = []
    ctx = DagRunContext(
        run_id="r",
        emit_event=lambda t, p: events.append((t, dict(p), time.monotonic())),
        get_run_status=AsyncMock(return_value="running"),
        pause_run=AsyncMock(),
        cancel_run=AsyncMock(),
        send_message=AsyncMock(),
        get_subgraph_yaml=lambda ref: None,
    )
    return ctx, events


def _logs(events):
    return [p for t, p, _ in events if t == "node_log"]


# ── stream_subprocess / bash / script ────────────────────────────────────────


def test_bash_streams_ordered_chunks_before_completion():
    ctx, events = _ctx()
    node = _node({"id": "b", "bash": "for i in 1 2 3; do echo $i; sleep 0.4; done"})
    res = _arun(execute_bash_node(node, {}, ctx))
    assert res.state == "completed" and res.output == "1\n2\n3"  # summary unchanged
    done_at = next(ts for t, _, ts in events if t == "node_completed")
    early = [p for t, p, ts in events if t == "node_log" and ts < done_at]
    assert len(early) >= 2
    logs = _logs(events)
    assert [p["seq_in_node"] for p in logs] == list(range(len(logs)))
    assert "".join(p["text"] for p in logs) == "1\n2\n3\n"
    assert all(p["node_id"] == "b" and p["stream"] == "stdout" for p in logs)


def test_large_output_capped_no_deadlock():
    ctx, events = _ctx()
    # 1MB on stdout AND stderr: a sequential reader would deadlock on stderr.
    node = _node({"id": "big", "bash": "head -c 1048576 /dev/zero | tr '\\0' a; "
                                       "head -c 1048576 /dev/zero | tr '\\0' b >&2; echo end",
                  "timeout": 20000})
    res = _arun(execute_bash_node(node, {}, ctx))
    assert res.state == "completed"
    assert len(res.output) == 1048576 + 3  # full output still returned
    logs = _logs(events)
    assert sum(len(p["text"].encode()) for p in logs) <= xs.NODE_LOG_MAX_BYTES
    assert logs[-1] == {**logs[-1], "truncated": True, "text": ""}
    assert sum(1 for p in logs if p.get("truncated")) == 1
    assert all(len(p["text"].encode()) <= 64 * 1024 for p in logs)


def test_timeout_still_kills_group():
    ctx, events = _ctx()
    node = _node({"id": "slow", "bash": "echo hi; sleep 30 & wait", "timeout": 600})
    t0 = time.monotonic()
    res = _arun(execute_bash_node(node, {}, ctx))
    assert res.state == "failed" and "timed out" in res.error
    assert time.monotonic() - t0 < 5
    assert not xs._LIVE_PGIDS


def test_cancel_mid_stream_kills_process(tmp_path):
    pidf = tmp_path / "pid"

    async def go():
        ctx, events = _ctx()
        node = _node({"id": "c", "bash": f"echo $$ > {pidf}; while true; do echo x; sleep 0.05; done"})
        task = asyncio.ensure_future(execute_bash_node(node, {}, ctx))
        while not _logs(events):
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return int(pidf.read_text())

    pid = _arun(go())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not xs._LIVE_PGIDS


def test_kill_switch_disables_log(monkeypatch):
    monkeypatch.setenv("WORKFLOW_NODE_LOG", "0")
    ctx, events = _ctx()
    res = _arun(execute_bash_node(_node({"id": "k", "bash": "echo hi"}), {}, ctx))
    assert res.output == "hi" and not _logs(events)


def test_script_node_streams():
    ctx, events = _ctx()
    node = _node({"id": "s", "runtime": "uv",
                  "script": "import sys\nprint('out')\nprint('err', file=sys.stderr)\n"})
    res = _arun(execute_script_node(node, {}, ctx))
    assert res.state == "completed" and res.output == "out"
    streams = {p["stream"]: p["text"] for p in _logs(events)}
    assert streams.get("stdout") == "out\n" and "err" in streams.get("stderr", "")


# ── store / bus ──────────────────────────────────────────────────────────────


def _store(path):
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return RunStore(conn), conn


def _seed_run(conn, run_id="run-1"):
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT OR IGNORE INTO workflow_definitions (id, name, description, source, yaml, checksum,"
        " created_at, updated_at, kind) VALUES ('wf','wf','wf','user','x','ck',?,?,'workflow')",
        (now, now),
    )
    conn.execute(
        "INSERT INTO workflow_runs (id, workflow_id, conversation_id, working_path, user_message,"
        " status, current_phase, metadata, started_at, last_heartbeat)"
        " VALUES (?,'wf','c','/tmp','m','running','plan','{}',?,?)",
        (run_id, now, now),
    )
    conn.commit()


def test_insert_event_returns_id_and_seq_and_bus_payload_has_them(tmp_path):
    store, conn = _store(tmp_path / "db.sqlite")
    _seed_run(conn)
    eid, seq = store.insert_event(workflow_run_id="run-1", event_type="x")
    assert isinstance(seq, int) and seq > 0
    assert store.insert_event(workflow_run_id="run-1", event_type="x", event_id=eid) == (eid, seq)

    bus = EventBus(run_store=store)

    async def go():
        gen = bus.subscribe("run-1")
        first = await gen.__anext__()  # replayed row carries seq too
        assert first["seq"] == seq and first["id"] == eid
        bus.emit(run_id="run-1", event_type="node_started", data={"node_id": "a"})
        live = await asyncio.wait_for(gen.__anext__(), 2)
        await gen.aclose()
        return live

    live = _arun(go())
    assert live["seq"] == seq + 1 and live["id"]


def test_replay_and_default_queries_exclude_node_log(tmp_path):
    store, conn = _store(tmp_path / "db.sqlite")
    _seed_run(conn)
    store.insert_event(workflow_run_id="run-1", event_type="node_started")
    for i in range(60):
        store.insert_event(workflow_run_id="run-1", event_type="node_log", data={"text": str(i)})
    assert [e["event_type"] for e in store.list_recent_events("run-1")] == ["node_started"]
    assert len(store.list_recent_events("run-1", types=["node_log"], limit=100)) == 60
    page = store.list_events_after("run-1", 0, 10, types=["node_log"])
    assert [e["data"]["text"] for e in page] == [str(i) for i in range(10)]
    assert store.max_event_rowid("run-1") == page[0]["seq"] + 59


def test_db_tail_delivers_cross_process_events_exactly_once(tmp_path):
    db = tmp_path / "db.sqlite"
    store, conn = _store(db)
    _seed_run(conn)
    store.insert_event(workflow_run_id="run-1", event_type="old")
    other, _ = _store(db)  # stands in for the gateway / daemon process
    bus = EventBus(run_store=store)

    async def go():
        gen = bus.subscribe("run-1", tail_interval_s=0.1)
        assert (await gen.__anext__())["event_type"] == "old"  # replay
        got = []

        async def collect():
            async for e in gen:
                got.append(e)

        t = asyncio.ensure_future(collect())
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        other.insert_event(workflow_run_id="run-1", event_type="remote")
        while not got:
            await asyncio.sleep(0.02)
        latency = time.monotonic() - t0
        # same-process emit: bus delivers it AND the tail sees the row
        bus.emit(run_id="run-1", event_type="local")
        await asyncio.sleep(0.5)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        await gen.aclose()
        return got, latency

    got, latency = _arun(go())
    assert latency < 1.0
    assert [e["event_type"] for e in got] == ["remote", "local"]
    assert len({e["seq"] for e in got}) == 2


def test_tail_skips_history_and_seen_set_is_bounded(tmp_path):
    store, conn = _store(tmp_path / "db.sqlite")
    _seed_run(conn)
    for i in range(3000):
        store.insert_event(workflow_run_id="run-1", event_type="node_log", data={"text": "x"})
    bus = EventBus(run_store=store)

    async def go():
        gen = bus.subscribe("run-1", tail_interval_s=0.05)
        nxt = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.3)
        assert not nxt.done()  # old node_log neither replayed nor tailed
        for _ in range(2500):
            bus.emit(run_id="run-1", event_type="node_log", data={"text": "y"})
        seqs = [(await nxt)["seq"]]
        for _ in range(2499):
            seqs.append((await asyncio.wait_for(gen.__anext__(), 2))["seq"])
        await asyncio.sleep(0.3)  # tail polls the same rows: all deduped
        extra = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.3)
        assert not extra.done()
        extra.cancel()
        await asyncio.gather(extra, return_exceptions=True)
        await gen.aclose()
        return seqs

    seqs = _arun(go())
    assert len(seqs) == len(set(seqs)) == 2500


# ── runner wiring + API ──────────────────────────────────────────────────────


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
    with TestClient(app) as c:
        yield c
    api_mod._engine = original


def _run_to_end(eng, yaml_text, wid="logwf"):
    async def go():
        await eng.upsert_definition(wid, yaml_text)
        run = await eng.start_run(wid, {}, {"kind": "manual"})
        await eng.wait_for_run(run["id"], timeout=20)
        return run["id"]
    return _arun(go())


_LOG_YAML = """\
name: logwf
description: d
nodes:
  - id: chat
    bash: "for i in $(seq 1 40); do echo line$i; sleep 0.02; done"
"""


def test_runner_attaches_node_run_and_api_pages(eng, client, monkeypatch):
    monkeypatch.setattr(xs, "NODE_LOG_FLUSH_BYTES", 16)  # many small chunks
    run_id = _run_to_end(eng, _LOG_YAML)
    nr = eng._run_store.find_node_run(run_id, "chat")
    assert nr["status"] == "completed" and nr["summary"].startswith("line1\n")

    default = client.get(f"/runs/{run_id}/events").json()
    assert "node_log" not in {e["event_type"] for e in default["events"]}
    assert all("seq" in e for e in default["events"])
    assert default["cursor"] == default["events"][-1]["seq"]

    q = f"/runs/{run_id}/events?type=node_log&node_run_id={nr['id']}"
    p1 = client.get(q + "&after=0&limit=3").json()
    assert len(p1["events"]) == 3
    assert all(e["node_run_id"] == nr["id"] and e["step_name"] == "chat" for e in p1["events"])
    p2 = client.get(q + f"&after={p1['cursor']}&limit=1000").json()
    seqs = [e["seq"] for e in p1["events"] + p2["events"]]
    assert seqs == sorted(seqs) and seqs[3] > p1["cursor"]
    text = "".join(e["data"]["text"] for e in p1["events"] + p2["events"])
    assert text == "".join(f"line{i}\n" for i in range(1, 41))
    empty = client.get(q + f"&after={seqs[-1]}").json()
    assert empty == {"events": [], "cursor": seqs[-1]}
    assert client.get(q + "&after=x").status_code == 400


def test_api_caps_node_log_text_and_rows(eng, client):
    run_id = _run_to_end(eng, _LOG_YAML)
    big = "z" * 20000
    for _ in range(1005):
        eng._run_store.insert_event(workflow_run_id=run_id, event_type="node_log", data={"text": big})
    body = client.get(f"/runs/{run_id}/events?type=node_log&limit=5000").json()
    assert len(body["events"]) == 1000
    assert all(len(e["data"]["text"]) <= 8192 for e in body["events"])
    assert body["events"][-1]["data"]["text_truncated"] is True


def test_health_lists_b3_features(client):
    body = client.get("/health").json()
    assert body["version"] == "0.3.0"
    assert {"node_log", "events_query", "sse_db_tail", "cross_process_sse"} <= set(body["features"])


def test_daily_retention_sweep(eng, monkeypatch):
    import engine.runtime.runner as runner_mod
    calls = []
    eng._runner.retention_days = 30
    monkeypatch.setattr(runner_mod, "RETENTION_SWEEP_S", 0.0)
    monkeypatch.setattr(eng._run_store, "delete_terminal_runs_older_than", lambda d: calls.append(d) or 0)

    async def go():
        t = asyncio.ensure_future(eng._runner.heartbeat_forever(interval_s=0.01))
        await asyncio.sleep(0.1)
        t.cancel()

    _arun(go())
    assert calls and set(calls) == {30}
