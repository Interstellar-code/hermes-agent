"""B2: prompt nodes routed to another profile via the gateway (MockTransport only)."""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import engine.nodes.agent_session as agent_session
from engine.core.dag_executor import DagRunContext, _execute_node_with_retry
from engine.core.executor_shared import classify_error
from engine.core.node_dispatcher import dispatch_node
from engine.db.client import open_db
from engine.db.migrate import ensure_schema
from engine.nodes.prompt import execute_prompt_node
from engine.schemas.dag_node import validate_dag_node
from engine.wiring import create_engine

KEY = "sk-test-routing-secret-0123456789"
IDEM_RE = re.compile(r"^wf:run-1:step:[0-9a-f]{12}$")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """HERMES_HOME (conftest) with a 'neo' profile holding an API key."""
    h = Path(os.environ["HERMES_HOME"])
    neo = h / "profiles" / "neo"
    neo.mkdir(parents=True)
    (neo / ".env").write_text(f"API_SERVER_KEY={KEY}\n")
    (h / "profiles" / "nokey").mkdir()
    write_cfg(h, enabled=True, allowed_profiles=["neo", "nokey", "default"])
    monkeypatch.setattr(agent_session, "POLL_S", 0)
    return h


def write_cfg(h: Path, **routing):
    lines = ["workflow:", "  routing:"]
    lines += [f"    {k}: {json.dumps(v)}" for k, v in routing.items()]
    (h / "config.yaml").write_text("\n".join(lines) + "\n")


@pytest.fixture()
def sleeps(monkeypatch):
    rec: list = []

    async def fake_sleep(s):
        rec.append(s)
        await asyncio.sleep(0)

    monkeypatch.setattr(agent_session, "_sleep", fake_sleep)
    return rec


@pytest.fixture()
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(agent_session, "_monotonic", lambda: now[0])
    return now


def node(**hermes_task):
    raw = {"id": "step", "prompt": "Say $topic", "hermes_task": {"profile": "neo", **hermes_task}}
    n, errs = validate_dag_node(raw, 0)
    assert not errs, errs
    return n


def make_ctx(h, llm=None):
    events: list = []
    ctx = DagRunContext(
        run_id="run-1",
        emit_event=lambda t, p: events.append((t, dict(p))),
        get_run_status=AsyncMock(return_value="running"),
        pause_run=AsyncMock(), cancel_run=AsyncMock(), send_message=AsyncMock(),
        get_subgraph_yaml=lambda ref: None, llm=llm,
    )
    ctx.home = str(h)
    ctx.cwd = "/work"
    ctx.workflow_vars = {"inputs": {"topic": "PONG"}}
    return ctx, events


class Gateway:
    """Scripted gateway. ``posts``: list of responses/exceptions for POST /runs;
    ``polls``: list of status dicts (last one repeats) or ints (HTTP code)."""

    def __init__(self, posts=None, polls=None, on_get=None):
        self.posts = list(posts or [httpx.Response(202, json={"run_id": "run_abc", "status": "queued"})])
        self.polls = list(polls or [{"status": "completed", "output": "PONG", "session_id": "run_abc",
                                     "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}])
        self.on_get = on_get
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/stop"):
            return httpx.Response(200, json={"status": "stopping"})
        if request.method == "POST":
            r = self.posts.pop(0) if len(self.posts) > 1 else self.posts[0]
            if isinstance(r, Exception):
                raise r
            return r
        if self.on_get:
            self.on_get()
        p = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        return httpx.Response(p, json={}) if isinstance(p, int) else httpx.Response(200, json=p)

    def transport(self):
        return httpx.MockTransport(self)

    def runs_posts(self):
        return [r for r in self.requests if r.method == "POST" and not r.url.path.endswith("/stop")]

    def stops(self):
        return [r for r in self.requests if r.url.path.endswith("/stop")]


async def run(gw, h, n=None, **kw):
    ctx, events = make_ctx(h)
    res = await agent_session.execute_agent_session_node(n or node(**kw), {}, ctx, transport=gw.transport())
    return res, events


# 1 ─ happy path, engine end-to-end (events + node_runs fields)
@pytest.mark.asyncio
async def test_happy_path_routes_and_records_session(home, sleeps, monkeypatch):
    gw = Gateway(polls=[
        {"status": "running", "session_id": "run_abc"},
        {"status": "completed", "output": "PONG", "session_id": "run_abc",
         "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}},
    ])
    monkeypatch.setattr(agent_session, "execute_agent_session_node", functools.partial(
        agent_session.execute_agent_session_node, transport=gw.transport()))
    eng = create_engine(db_path=str(home / "wf.db"), seed_bundled=False,
                        write_manifest=False, crash_recovery=False)
    llm = MagicMock()
    eng.set_llm(llm)
    await eng.upsert_definition("routed", (
        "name: routed\ndescription: d\nnodes:\n"
        "  - id: step\n    prompt: 'Reply PONG'\n    systemPrompt: be brief\n"
        "    hermes_task: {profile: neo, timeout_s: 300}\n"))
    run_id = (await eng.start_run("routed", {}, {}))["id"]
    end = time.monotonic() + 10
    while (r := await eng.get_run(run_id))["status"] not in ("completed", "failed"):
        assert time.monotonic() < end
        await asyncio.sleep(0.02)
    assert r["status"] == "completed", r
    llm.complete.assert_not_called()

    post = gw.runs_posts()[0]
    assert post.url == httpx.URL("http://127.0.0.1:8642/p/neo/v1/runs")
    assert post.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(post.content)
    assert body["input"] == "Reply PONG"
    assert body["instructions"].startswith(f"Workflow step 'step' of run {run_id}. Working directory: ")
    assert body["instructions"].endswith("\n\nbe brief")

    (nr,) = await eng.list_node_runs(run_id)
    assert nr["status"] == "completed" and nr["summary"] == "PONG"
    assert (nr["assigned_agent"], nr["session_id"], nr["gateway_run_id"]) == ("neo", "run_abc", "run_abc")
    assert (nr["input_tokens"], nr["output_tokens"], nr["total_tokens"]) == (10, 5, 15)
    assert nr["cost_usd"] is None and nr["model"] is None

    events = await eng.list_recent_workflow_events(run_id)
    (started,) = [e for e in events if e["event_type"] == "node_session_started"]
    assert started["data"] == {"run_id": run_id, "node_id": "step", "profile": "neo",
                               "session_id": "run_abc", "gateway_run_id": "run_abc"}
    assert started["node_run_id"] == nr["id"]


# 2 ─ flag off → local prompt path, byte-identical, no HTTP
@pytest.mark.asyncio
async def test_flag_off_runs_prompt_path_identically(home):
    write_cfg(home, enabled=False, allowed_profiles=["neo"])
    gw = Gateway()

    def llm():
        m = MagicMock()
        m.complete.return_value = MagicMock(text="local", usage=None)
        return m

    ctx_a, ev_a = make_ctx(home, llm())
    ctx_b, ev_b = make_ctx(home, llm())
    n = node()
    ra = await agent_session.execute_agent_session_node(n, {}, ctx_a, transport=gw.transport())
    rb = await execute_prompt_node(n, {}, ctx_b)
    assert gw.requests == []
    assert ra == rb and ra.state == "completed" and ra.output == "local"
    strip = lambda ev: [(t, {k: v for k, v in p.items() if k != "duration_ms"}) for t, p in ev]  # noqa: E731
    assert strip(ev_a) == strip(ev_b)
    assert ctx_a.llm.complete.call_args == ctx_b.llm.complete.call_args

    (home / "config.yaml").unlink()  # missing config == disabled
    ctx_c, _ = make_ctx(home, llm())
    assert (await agent_session.execute_agent_session_node(n, {}, ctx_c, transport=gw.transport())).output == "local"
    assert gw.requests == []


# 3 ─ allowlist deny: FATAL, no HTTP, no DAG retry
@pytest.mark.asyncio
async def test_allowlist_deny_is_fatal_without_http(home):
    write_cfg(home, enabled=True, allowed_profiles=["other"])
    gw = Gateway()
    calls = []

    async def dispatch(n, outs, ctx):
        calls.append(1)
        return await agent_session.execute_agent_session_node(n, outs, ctx, transport=gw.transport())

    ctx, events = make_ctx(home)
    n = node()
    n.retry = validate_dag_node({"id": "x", "prompt": "p", "retry": {"max_attempts": 3, "on_error": "all"}}, 0)[0].retry
    res = await _execute_node_with_retry(n, 0, False, {}, ctx, dispatch)
    assert res.state == "failed" and "forbidden" in res.error
    assert classify_error(res.error) == "FATAL"
    assert calls == [1] and gw.requests == []
    assert [t for t, _ in events] == ["node_started", "node_failed"]


# 4 ─ missing key / profile default / missing dir → FATAL
@pytest.mark.asyncio
async def test_missing_key_default_profile_and_missing_dir_fatal(home):
    gw = Gateway()
    for profile in ("nokey", "default", "ghost"):
        write_cfg(home, enabled=True, allowed_profiles=[profile])
        n = node()
        n.hermes_task.profile = profile  # bypass parse-time rejection of 'default'
        res, _ = await run(gw, home, n)
        assert res.state == "failed" and classify_error(res.error) == "FATAL", (profile, res.error)
    assert gw.requests == []
    _, errs = validate_dag_node({"id": "a", "prompt": "p", "hermes_task": {"profile": "default"}}, 0)
    assert errs and "'default'" in errs[0]
    _, errs = validate_dag_node({"id": "a", "prompt": "p", "hermes_task": {"profile": "../etc"}}, 0)
    assert errs


# 5 ─ idempotency key: format, stable across a dispatch's retries, new per DAG retry
@pytest.mark.asyncio
async def test_idempotency_key_per_dispatch(home, sleeps):
    gw = Gateway(posts=[
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.ConnectError("refused"),
        httpx.Response(202, json={"run_id": "run_abc"}),
    ])
    res, _ = await run(gw, home)
    assert res.state == "completed"
    keys = {r.headers["Idempotency-Key"] for r in gw.runs_posts()}
    assert len(gw.runs_posts()) == 3 and len(keys) == 1
    (first,) = keys
    assert IDEM_RE.match(first)
    # Polls and stop reuse the dispatch's headers too.
    assert all(r.headers["Idempotency-Key"] == first for r in gw.requests)

    # DAG retry: first dispatch exhausts 429 (TRANSIENT) → executor re-dispatches.
    gw2 = Gateway(posts=[httpx.Response(429)] * 6 + [httpx.Response(202, json={"run_id": "run_abc"})])
    monkeypatch_dispatch = functools.partial(agent_session.execute_agent_session_node, transport=gw2.transport())
    ctx, _ = make_ctx(home)
    n = node()
    n.retry = validate_dag_node({"id": "x", "prompt": "p", "retry": {"max_attempts": 1, "delay_ms": 1000}}, 0)[0].retry
    res = await _execute_node_with_retry(n, 0, False, {}, ctx, lambda nn, o, c: monkeypatch_dispatch(nn, o, c))
    assert res.state == "completed"
    k = [r.headers["Idempotency-Key"] for r in gw2.runs_posts()]
    assert len(k) == 7 and len(set(k[:6])) == 1 and k[6] != k[0]
    assert all(IDEM_RE.match(x) for x in k) and first not in k


# 6 ─ 429 → 202 honours Retry-After; exhausted is TRANSIENT
@pytest.mark.asyncio
async def test_rate_limit_backoff(home, sleeps):
    gw = Gateway(posts=[httpx.Response(429, headers={"Retry-After": "3"}),
                        httpx.Response(202, json={"run_id": "run_abc"})])
    res, _ = await run(gw, home)
    assert res.state == "completed" and sleeps[0] == 3.0

    sleeps.clear()
    gw = Gateway(posts=[httpx.Response(429)])
    res, _ = await run(gw, home)
    assert res.state == "failed" and classify_error(res.error) == "TRANSIENT"
    assert sleeps == [1, 2, 4, 8, 16] and len(gw.runs_posts()) == 6


# 7 ─ cancel → POST /stop, CancelledError re-raised
@pytest.mark.asyncio
async def test_cancel_stops_gateway_run(home, sleeps):
    gw = Gateway(polls=[{"status": "running", "session_id": "run_abc"}])
    ctx, events = make_ctx(home)
    task = asyncio.create_task(
        agent_session.execute_agent_session_node(node(), {}, ctx, transport=gw.transport()))
    while not any(t == "node_session_started" for t, _ in events):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (stop,) = gw.stops()
    assert stop.url.path == "/p/neo/v1/runs/run_abc/stop"


# 8 ─ timeout_s → stop + fail, not TRANSIENT
@pytest.mark.asyncio
async def test_time_limit_stops_and_fails(home, sleeps, clock):
    def tick():
        clock[0] += 100
    gw = Gateway(polls=[{"status": "running", "session_id": "run_abc"}], on_get=tick)
    res, _ = await run(gw, home, timeout_s=300)
    assert res.state == "failed" and "time limit" in res.error
    assert classify_error(res.error) != "TRANSIENT"
    assert len(gw.stops()) == 1


# 9 ─ approval wait exceeded → stop + fail
@pytest.mark.asyncio
async def test_approval_wait_exceeded(home, sleeps, clock):
    write_cfg(home, enabled=True, allowed_profiles=["neo"], approval_wait_s=50)

    def tick():
        clock[0] += 30
    gw = Gateway(polls=[{"status": "running"}, {"status": "waiting_for_approval"}], on_get=tick)
    res, _ = await run(gw, home)
    assert res.state == "failed" and "approval wait exceeded" in res.error
    assert classify_error(res.error) != "TRANSIENT"
    assert len(gw.stops()) == 1


# 10 ─ gateway down → TRANSIENT after 3 same-key retries
@pytest.mark.asyncio
async def test_gateway_down_transient(home, sleeps):
    gw = Gateway(posts=[httpx.ConnectError("refused")])
    res, _ = await run(gw, home)
    assert res.state == "failed" and classify_error(res.error) == "TRANSIENT"
    assert len(gw.runs_posts()) == 4
    assert len({r.headers["Idempotency-Key"] for r in gw.runs_posts()}) == 1


# 11 ─ poll 404 and interrupted → fail (not TRANSIENT); failure usage kept
@pytest.mark.asyncio
async def test_poll_404_and_interrupted(home, sleeps):
    res, _ = await run(Gateway(polls=[{"status": "running"}, 404]), home)
    assert res.state == "failed" and "routed run vanished" in res.error

    gw = Gateway(polls=[{"status": "interrupted", "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}}])
    res, events = await run(gw, home)
    assert res.error.endswith("interrupted (gateway restarted)")
    assert classify_error(res.error) != "TRANSIENT"
    failed = [p for t, p in events if t == "node_failed"][0]
    assert failed["usage"]["total_tokens"] == 4 and failed["usage"]["cost_usd"] is None


# 12 ─ 409 → fail, no retry inside the dispatch
@pytest.mark.asyncio
async def test_conflict_409_fails(home, sleeps):
    gw = Gateway(posts=[httpx.Response(409, json={"error": "conflict"})])
    res, _ = await run(gw, home)
    assert res.state == "failed" and "409" in res.error
    assert classify_error(res.error) != "TRANSIENT"
    assert len(gw.runs_posts()) == 1


# 13 ─ non-loopback gateway_url → FATAL, no HTTP
@pytest.mark.asyncio
async def test_non_loopback_gateway_url_fatal(home):
    gw = Gateway()
    for url in ("http://10.0.0.5:8642", "http://127.0.0.1.evil.example:8642", "ftp://127.0.0.1",
                "http://localhost:8642", "http://[::1"):
        write_cfg(home, enabled=True, allowed_profiles=["neo"], gateway_url=url)
        res, _ = await run(gw, home)
        assert res.state == "failed" and classify_error(res.error) == "FATAL", url
        assert "misconfigured" in res.error
    assert gw.requests == []


async def _retry_all(home, gw, **kw):
    calls = []

    async def dispatch(n, outs, ctx):
        calls.append(1)
        return await agent_session.execute_agent_session_node(n, outs, ctx, transport=gw.transport())

    ctx, _ = make_ctx(home)
    n = node(**kw)
    n.retry = SimpleNamespace(max_attempts=2, delay_ms=1, on_error="all")
    res = await _execute_node_with_retry(n, 0, False, {}, ctx, dispatch)
    return res, calls


# 16 ─ read/write timeout on dispatch: may have started, never DAG-retried
@pytest.mark.asyncio
async def test_dispatch_read_timeout_not_retried(home, sleeps):
    gw = Gateway(posts=[httpx.ReadTimeout("slow")])
    res, calls = await _retry_all(home, gw)
    assert res.state == "failed" and "may have started" in res.error
    assert "connection refused" not in res.error and classify_error(res.error) != "TRANSIENT"
    assert len(gw.runs_posts()) == 4 and calls == [1]
    assert len({r.headers["Idempotency-Key"] for r in gw.runs_posts()}) == 1


# 17 ─ non-retryable routed failures survive on_error: all
@pytest.mark.asyncio
async def test_no_retry_failures_ignore_on_error_all(home, sleeps, clock):
    def tick():
        clock[0] += 100
    for gw, kw in (
        (Gateway(polls=[{"status": "running"}], on_get=tick), {"timeout_s": 300}),
        (Gateway(posts=[httpx.Response(401)]), {}),
        (Gateway(polls=[500]), {}),
    ):
        res, calls = await _retry_all(home, gw, **kw)
        assert res.state == "failed" and calls == [1], res.error
    # gateway-down stays retryable under on_error: all
    res, calls = await _retry_all(home, Gateway(posts=[httpx.ConnectError("x")]))
    assert len(calls) == 3


# 18 ─ non-JSON bodies: poll parse error counts as poll error; dispatch parse error aborts
@pytest.mark.asyncio
async def test_non_json_bodies(home, sleeps):
    class NonJsonPolls(Gateway):
        def __call__(self, request):
            if request.method == "GET":
                self.requests.append(request)
                return httpx.Response(200, content=b"<html>")
            return super().__call__(request)

    gw = NonJsonPolls()
    res, _ = await run(gw, home)
    assert res.state == "failed" and "lost contact" in res.error
    assert len(gw.stops()) == 1

    gw = Gateway(posts=[httpx.Response(202, content=b"nope")])
    res, _ = await run(gw, home)
    assert res.state == "failed" and "no run_id" in res.error


# 19 ─ unexpected exception after dispatch → stop + non-retryable abort
@pytest.mark.asyncio
async def test_unexpected_exception_aborts_and_stops(home, sleeps):
    def boom():
        raise RuntimeError("kaboom")
    gw = Gateway(on_get=boom)
    res, calls = await _retry_all(home, gw)
    assert res.state == "failed" and res.error == "routed run on 'neo' aborted: RuntimeError"
    assert len(gw.stops()) == 1 and calls == [1]


# 20 ─ Retry-After: non-finite / negative / huge → sane delay
@pytest.mark.asyncio
async def test_retry_after_sanitised(home, sleeps):
    for ra, want in (("nan", 1), ("inf", 1), ("-5", 0.0), ("999", 30.0), ("junk", 1)):
        sleeps.clear()
        gw = Gateway(posts=[httpx.Response(429, headers={"Retry-After": ra}),
                            httpx.Response(202, json={"run_id": "run_abc"})])
        res, _ = await run(gw, home)
        assert res.state == "completed" and sleeps[0] == want, ra


# 21 ─ node_started on an existing node_run clears the previous attempt's session
@pytest.mark.asyncio
async def test_node_started_reuse_clears_session(home, sleeps, monkeypatch):
    gw = Gateway(posts=[httpx.Response(202, json={"run_id": "run_1"}),
                        httpx.Response(202, json={"run_id": "run_2"})],
                 polls=[{"status": "failed", "error": "timeout upstream", "session_id": "run_1"},
                        {"status": "running", "session_id": "run_2"}])
    monkeypatch.setattr(agent_session, "execute_agent_session_node", functools.partial(
        agent_session.execute_agent_session_node, transport=gw.transport()))
    eng = create_engine(db_path=str(home / "wf.db"), seed_bundled=False,
                        write_manifest=False, crash_recovery=False)
    eng.set_llm(MagicMock())
    await eng.upsert_definition("routed", (
        "name: routed\ndescription: d\nnodes:\n"
        "  - id: step\n    prompt: 'x'\n    hermes_task: {profile: neo}\n"
        "    retry: {max_attempts: 1, delay_ms: 1000, on_error: all}\n"))
    run_id = (await eng.start_run("routed", {}, {}))["id"]
    end = time.monotonic() + 10
    while len(gw.runs_posts()) < 2:
        assert time.monotonic() < end
        await asyncio.sleep(0.01)
    # attempt 2 dispatched: its node_started already reset the row to attempt 1's leftovers -> None
    (nr,) = await eng.list_node_runs(run_id)
    assert nr["gateway_run_id"] in (None, "run_2") and nr["gateway_run_id"] != "run_1"
    assert nr["session_id"] != "run_1"
    await eng.cancel_run(run_id)


# 14 ─ key never in logs, events or errors
@pytest.mark.asyncio
async def test_key_never_leaks(home, sleeps, caplog):
    caplog.set_level(logging.DEBUG)
    seen = []
    for gw in (Gateway(), Gateway(posts=[httpx.Response(409)]), Gateway(posts=[httpx.ConnectError("x")]),
               Gateway(polls=[{"status": "failed", "error": "boom"}])):
        res, events = await run(gw, home)
        seen.append(repr(res))
        seen.append(repr(events))
    assert all(KEY not in s for s in seen)
    assert KEY not in caplog.text


# 15 ─ parse error: profile on non-prompt nodes
def test_profile_rejected_on_non_prompt_nodes():
    for raw in (
        {"id": "b", "bash": "echo", "hermes_task": {"profile": "neo"}},
        {"id": "l", "loop": {"prompt": "p", "until": "DONE", "max_iterations": 2}, "hermes_task": {"profile": "neo"}},
        {"id": "c", "command": "do-it", "hermes_task": {"profile": "neo"}},
    ):
        n, errs = validate_dag_node(raw, 0)
        assert n is None and errs == [f"Node '{raw['id']}': hermes_task.profile is only supported on prompt nodes"]
    n, errs = validate_dag_node({"id": "p", "prompt": "x", "hermes_task": {"profile": "neo", "timeout_s": 60}}, 0)
    assert not errs and n.hermes_task.timeout_s == 60
    assert validate_dag_node({"id": "p", "prompt": "x", "hermes_task": {"profile": "neo", "timeout_s": 0}}, 0)[1]


def test_unrouted_prompt_dispatches_locally(home):
    """No hermes_task.profile → prompt path even with routing enabled."""
    n, _ = validate_dag_node({"id": "p", "prompt": "x"}, 0)
    llm = MagicMock()
    llm.complete.return_value = MagicMock(text="local", usage=None)
    ctx, _ = make_ctx(home, llm)
    ctx.log_dir = None
    assert asyncio.run(dispatch_node(n, {}, ctx)).output == "local"


def test_migrate_008_fresh_and_half_applied():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        assert int(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]) >= 8
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_runs)")}
        assert {"session_id", "gateway_run_id"} <= cols
        conn.execute("ALTER TABLE node_runs DROP COLUMN gateway_run_id")
        conn.execute("UPDATE schema_meta SET value='7' WHERE key='schema_version'")
        conn.commit()
        ensure_schema(conn)  # session_id already present: must converge
        assert conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "8"
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_runs)")}
        assert {"session_id", "gateway_run_id"} <= cols


def test_health_routing_and_active_node_runs(home, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import plugins.workflow_engine.dashboard.plugin_api as api_mod

    write_cfg(home, enabled=True, allowed_profiles=["neo"], gateway_url="http://127.0.0.1:8642")
    eng = create_engine(db_path=str(home / "wf.db"), seed_bundled=False,
                        write_manifest=False, crash_recovery=False)
    monkeypatch.setattr(api_mod, "_engine", lambda: eng)
    app = FastAPI()
    app.include_router(api_mod.router)
    store = eng._run_store
    eng._conn.execute(
        "INSERT INTO workflow_definitions (id, name, yaml, source, checksum, created_at, updated_at) "
        "VALUES ('wf','WF','x','user','x',0,0)")
    r = store.create_workflow_run(workflow_id="wf", conversation_id="c", working_path="/", user_message="m")
    nr = store.create_node_run(workflow_run_id=r["id"], dag_node_id="step", node_type="prompt")
    store.update_node_run(nr["id"], {"status": "running", "assigned_agent": "neo",
                                     "session_id": "run_abc", "gateway_run_id": "run_abc"})
    with TestClient(app) as c:
        health = c.get("/health").json()
        assert health["routing"] == {"enabled": True, "allowed_profiles": ["neo"]}
        assert "8642" not in json.dumps(health) and KEY not in json.dumps(health)
        (row,) = c.get("/node-runs/active").json()["nodeRuns"]
        assert (row["workerId"], row["sessionId"], row["gatewayRunId"]) == ("neo", "run_abc", "run_abc")
        write_cfg(home, enabled=False)
        assert c.get("/health").json()["routing"] == {"enabled": False, "allowed_profiles": []}
