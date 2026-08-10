"""Stopping a turn started on the sessions chat stream.

``POST /v1/runs/{run_id}/stop`` only ever worked for runs created by
``POST /v1/runs``: that handler is the only one that published the live agent
into ``_active_run_agents``. ``POST /api/sessions/{id}/chat/stream`` called
``_run_agent`` without ever registering anything, so its runs were invisible to
stop and the endpoint 404'd — the same shape as the approval gap, on the same
transport. These tests pin the runtime half of the fix:

* a sessions-stream run is registered, reachable from ``/stop``, and actually
  interrupted;
* ``GET /v1/runs/{run_id}`` answers for it from the first frame onwards, so the
  documented ``stopping`` → ``cancelled`` poll is usable;
* the registry is released on success, on exception, and on a client
  disconnect — from the executor thread, which outlives the asyncio wrapper;
* a stop landing during an approval wait leaves no orphan;
* a second stop is idempotent;
* ``stopping`` is *reported* as wedged rather than force-cancelled, and the run
  keeps its concurrency slot while it is; and
* a stop that lands after a successful turn keeps the output instead of
  discarding an answer the transcript already persisted.
"""

import asyncio
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import hermes_state
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from tools import approval as approval_mod


AUTH = {"Authorization": "Bearer test-key"}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def session_db(monkeypatch, _isolate_hermes_home):
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
    db = SessionDB()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def adapter(session_db):
    ad = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-key"}))
    ad._session_db = session_db
    return ad


@pytest.fixture(autouse=True)
def _clean_approval_registry():
    """The approval module keeps process-global registries."""
    yield
    with approval_mod._lock:
        approval_mod._gateway_notify_cbs.clear()
        approval_mod._gateway_queues.clear()


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    for method, path, handler in (
        ("POST", "/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream),
        ("POST", "/v1/runs", adapter._handle_runs),
        ("GET", "/v1/runs/{run_id}", adapter._handle_get_run),
        ("POST", "/v1/runs/{run_id}/stop", adapter._handle_stop_run),
        ("POST", "/v1/runs/{run_id}/approval", adapter._handle_run_approval),
        ("GET", "/v1/approvals/pending", adapter._handle_list_pending_approvals),
    ):
        app.router.add_route(method, path, handler)
    return app


async def _wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    frames = []
    for block in body.split("\n\n"):
        name = payload = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
        if name is not None and payload is not None:
            frames.append((name, payload))
    return frames


def _slow_agent(result=None, *, honours_interrupt: bool = True):
    """A mock agent that blocks in run_conversation until interrupt() lands.

    ``honours_interrupt=False`` models the uncooperative executor: the flag is
    set but the turn runs to a normal, complete result anyway — the shape of
    both a non-interrupt-aware tool and a stop that lost the race against a
    turn that had already finished.
    """
    ready = threading.Event()
    interrupted = threading.Event()
    release = threading.Event()
    agent = MagicMock()

    def _do_interrupt(message=None):
        interrupted.set()

    agent.interrupt = MagicMock(side_effect=_do_interrupt)

    def _run_conversation(**_kwargs):
        ready.set()
        if honours_interrupt:
            interrupted.wait(timeout=10)
            return result if result is not None else {
                "final_response": "partial answer",
                "interrupted": True,
            }
        release.wait(timeout=10)
        return result if result is not None else {"final_response": "full answer"}

    agent.run_conversation.side_effect = _run_conversation
    agent.session_prompt_tokens = 3
    agent.session_completion_tokens = 5
    agent.session_total_tokens = 8
    agent.session_id = None
    return agent, ready, interrupted, release


async def _start_stream(cli, session_id: str, message: str = "go"):
    resp = await cli.post(
        f"/api/sessions/{session_id}/chat/stream",
        json={"message": message},
        headers=AUTH,
    )
    assert resp.status == 200
    return resp


# ---------------------------------------------------------------------------
# 1 — the run is registered, findable, and genuinely interrupted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_stream_run_is_stoppable_and_interrupts_the_agent(adapter, session_db):
    session_id = session_db.create_session("stoppable", "api_server")
    agent, ready, _interrupted, _release = _slow_agent()

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            resp = await _start_stream(cli, session_id)
            body_task = asyncio.ensure_future(resp.text())

            run_id = await _wait_for(lambda: next(iter(adapter._run_statuses), None))
            await _wait_for(lambda: run_id in adapter._active_run_agents)

            # The status channel answers before the stop, which is what makes
            # the documented "poll after the 200" contract usable here.
            status = await cli.get(f"/v1/runs/{run_id}", headers=AUTH)
            assert status.status == 200
            assert (await status.json())["status"] == "running"

            stop = await cli.post(f"/v1/runs/{run_id}/stop", headers=AUTH)
            assert stop.status == 200
            assert await stop.json() == {"run_id": run_id, "status": "stopping"}

            agent.interrupt.assert_called_once_with("Stop requested via API")

            body = await asyncio.wait_for(body_task, timeout=10)

    await _wait_for(
        lambda: (adapter._run_statuses[run_id]["status"] == "cancelled")
    )
    final = adapter._run_statuses[run_id]
    assert final["status"] == "cancelled"
    assert final["interrupted"] is True
    # The partial answer the transcript kept is on the record, not discarded.
    assert final["output"] == "partial answer"

    frames = dict(_parse_sse(body))
    assert frames["assistant.completed"]["interrupted"] is True
    assert frames["assistant.completed"]["partial"] is True


@pytest.mark.asyncio
async def test_stop_before_the_agent_exists_is_accepted_and_prevents_the_turn(adapter, session_db):
    """The pre-agent window must not 404 while the status already says running.

    _create_agent runs inside the executor and takes seconds (toolset load,
    memory provider). POST /v1/runs covers that gap by registering its task up
    front; the sessions stream now does the same.
    """
    session_id = session_db.create_session("pre-agent", "api_server")
    gate = asyncio.Event()
    real_history = adapter._conversation_history_for_session

    async def _slow_history(sid):
        await gate.wait()
        return await real_history(sid)

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_conversation_history_for_session", side_effect=_slow_history), \
             patch.object(adapter, "_create_agent") as create_agent:
            resp = await _start_stream(cli, session_id)
            body_task = asyncio.ensure_future(resp.text())

            run_id = await _wait_for(lambda: next(iter(adapter._run_statuses), None))
            assert run_id in adapter._active_run_tasks
            assert run_id not in adapter._active_run_agents

            stop = await cli.post(f"/v1/runs/{run_id}/stop", headers=AUTH)
            assert stop.status == 200

            gate.set()
            await asyncio.wait_for(body_task, timeout=10)

    create_agent.assert_not_called()
    assert adapter._run_statuses[run_id]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 2 — the registry is released on every exit path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_is_released_on_success(adapter, session_db):
    session_id = session_db.create_session("clean-success", "api_server")
    agent = MagicMock()
    agent.run_conversation.return_value = {"final_response": "done"}
    agent.session_prompt_tokens = 1
    agent.session_completion_tokens = 2
    agent.session_total_tokens = 3
    agent.session_id = None

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            resp = await _start_stream(cli, session_id)
            await asyncio.wait_for(resp.text(), timeout=10)
            run_id = next(iter(adapter._run_statuses))

    assert run_id not in adapter._active_run_agents
    assert run_id not in adapter._active_run_tasks
    assert run_id not in adapter._stopping_run_ids
    assert adapter._run_statuses[run_id]["status"] == "completed"
    assert adapter._run_statuses[run_id]["interrupted"] is False
    # A finished run is no longer stoppable — this is the "nothing to stop"
    # signal the UI pairs with a 200 from GET /v1/runs/{id}.
    async with TestClient(TestServer(_app(adapter))) as cli:
        assert (await cli.post(f"/v1/runs/{run_id}/stop", headers=AUTH)).status == 404


@pytest.mark.asyncio
async def test_registry_is_released_when_the_agent_raises(adapter, session_db):
    session_id = session_db.create_session("clean-exception", "api_server")
    agent = MagicMock()
    agent.run_conversation.side_effect = RuntimeError("boom")
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_id = None

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            resp = await _start_stream(cli, session_id)
            await asyncio.wait_for(resp.text(), timeout=10)
            run_id = next(iter(adapter._run_statuses))

    assert run_id not in adapter._active_run_agents
    assert run_id not in adapter._active_run_tasks
    assert adapter._run_statuses[run_id]["status"] == "failed"


@pytest.mark.asyncio
async def test_disconnect_keeps_the_run_stoppable_then_releases_it(adapter, session_db, monkeypatch):
    """A browser going away must not make a live agent unstoppable.

    The SSE writer cancels the asyncio wrapper on disconnect, but the executor
    thread keeps running the turn. Registration and release therefore both live
    on that thread, exactly like the approval bookkeeping.
    """
    monkeypatch.setattr(
        "gateway.platforms.api_server.CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS", 0.05
    )
    session_id = session_db.create_session("disconnect-stop", "api_server")
    agent, ready, _interrupted, _release = _slow_agent()

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            resp = await _start_stream(cli, session_id)
            run_id = await _wait_for(lambda: next(iter(adapter._run_statuses), None))
            await _wait_for(lambda: run_id in adapter._active_run_agents)

            resp.close()
            await _wait_for(lambda: (None, session_id) not in adapter._clarify_streams)

            # The wrapper is gone; the agent is not.
            assert run_id in adapter._active_run_agents

            stop = await cli.post(f"/v1/runs/{run_id}/stop", headers=AUTH)
            assert stop.status == 200
            agent.interrupt.assert_called_once_with("Stop requested via API")

            await _wait_for(lambda: run_id not in adapter._active_run_agents)

    assert run_id not in adapter._active_run_tasks
    assert run_id not in adapter._stopping_run_ids
    assert adapter._run_statuses[run_id]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 3 — stop during an approval wait
# ---------------------------------------------------------------------------


_TERMINAL_APPROVAL = {
    "command": "cp payload /etc/systemd/system/x.service",
    "pattern_key": "system-config-write",
    "pattern_keys": ["system-config-write"],
    "description": "copy/move file into system config path",
    "allow_permanent": True,
}


@pytest.mark.asyncio
async def test_stop_during_an_approval_wait_leaves_no_orphan(adapter, session_db):
    """_await_gateway_decision self-resolves as "deny" when interrupted.

    The wait polls is_interrupted() on the agent's execution thread, so a stop
    releases it within ~1s and drops its own queue entry. Nothing may be left
    behind in the approval registries or the run's approval bookkeeping.
    """
    session_id = session_db.create_session("stop-approval", "api_server")
    seen: dict = {}
    agent = MagicMock()
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_id = None

    def _do_interrupt(message=None):
        # interrupt() sets a per-thread flag on the agent's execution thread;
        # the approval wait polls is_interrupted() on that same thread.
        from tools.interrupt import set_interrupt

        set_interrupt(True, seen["thread_id"])

    agent.interrupt = MagicMock(side_effect=_do_interrupt)

    def _run_conversation(**_kwargs):
        seen["thread_id"] = threading.get_ident()
        session_key = approval_mod.get_current_session_key()
        with approval_mod._lock:
            notify_cb = approval_mod._gateway_notify_cbs.get(session_key)
        assert notify_cb is not None, "adapter must register the notify callback"
        seen["decision"] = approval_mod._await_gateway_decision(
            session_key, notify_cb, dict(_TERMINAL_APPROVAL), surface="gateway"
        )
        return {"final_response": "denied and stopped", "interrupted": True}

    agent.run_conversation.side_effect = _run_conversation

    try:
        async with TestClient(TestServer(_app(adapter))) as cli:
            with patch.object(adapter, "_create_agent", return_value=agent), \
                 patch("tools.approval._get_approval_timeout", return_value=120):
                resp = await _start_stream(cli, session_id, "install the unit file")
                body_task = asyncio.ensure_future(resp.text())

                run_id = await _wait_for(
                    lambda: next(iter(adapter._run_approval_requests), None)
                )
                await _wait_for(lambda: run_id in adapter._active_run_agents)

                # The pending approval is advertised before the stop …
                listing = await cli.get("/v1/approvals/pending", headers=AUTH)
                assert len((await listing.json())["approvals"]) == 1

                stop = await cli.post(f"/v1/runs/{run_id}/stop", headers=AUTH)
                assert stop.status == 200

                await asyncio.wait_for(body_task, timeout=10)
    finally:
        if "thread_id" in seen:
            from tools.interrupt import set_interrupt

            set_interrupt(False, seen["thread_id"])

    assert seen["decision"]["choice"] == "deny"
    # No orphan: not in the approval core, not in the run bookkeeping, and no
    # longer advertised as pending.
    with approval_mod._lock:
        assert not approval_mod._gateway_queues
    assert run_id not in adapter._run_approval_requests
    assert run_id not in adapter._run_approval_sessions
    assert run_id not in adapter._active_run_agents
    assert adapter._run_statuses[run_id]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 4 — idempotence and the bound on "stopping"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_stop_is_idempotent(adapter, session_db):
    """Two stops on a live run answer identically.

    The agent here does not honour the interrupt, so the run is still
    registered for the second call. Once it is gone, stop 404s — the
    "already finished" case, pinned separately.
    """
    session_id = session_db.create_session("double-stop", "api_server")
    agent, ready, _interrupted, release = _slow_agent(honours_interrupt=False)

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            resp = await _start_stream(cli, session_id)
            body_task = asyncio.ensure_future(resp.text())
            run_id = await _wait_for(lambda: next(iter(adapter._run_statuses), None))
            await _wait_for(lambda: run_id in adapter._active_run_agents)

            first = await cli.post(f"/v1/runs/{run_id}/stop", headers=AUTH)
            first_stamp = adapter._run_statuses[run_id]["stop_requested_at"]
            await asyncio.sleep(0.05)
            second = await cli.post(f"/v1/runs/{run_id}/stop", headers=AUTH)

            assert await first.json() == {"run_id": run_id, "status": "stopping"}
            assert await second.json() == {"run_id": run_id, "status": "stopping"}
            assert agent.interrupt.call_count == 2
            # The wedged clock measures the whole stopping window, not the time
            # since the most recent button press.
            assert adapter._run_statuses[run_id]["stop_requested_at"] == first_stamp

            release.set()
            await asyncio.wait_for(body_task, timeout=10)

    assert adapter._run_statuses[run_id]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_wedged_stop_is_reported_not_force_cancelled(adapter, session_db):
    """"stopping" has no deadline, and that is deliberate.

    The turn runs on a run_in_executor thread; cancelling the asyncio wrapper
    would abandon it, not stop it, while the agent kept writing the transcript
    and holding its real resources. So the bound is on the *reporting*: the
    status says how long the stop has been pending and whether the agent has
    stopped honouring it, and the run keeps its concurrency slot until it
    genuinely returns.
    """
    session_id = session_db.create_session("wedged", "api_server")
    adapter._RUN_STOP_WEDGED_SECONDS = 0.05
    # Ignores interrupt() entirely — the non-interrupt-aware tool case.
    agent, ready, _interrupted, release = _slow_agent(honours_interrupt=False)

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            resp = await _start_stream(cli, session_id)
            body_task = asyncio.ensure_future(resp.text())
            run_id = await _wait_for(lambda: next(iter(adapter._run_statuses), None))
            await _wait_for(lambda: run_id in adapter._active_run_agents)

            assert await cli.post(f"/v1/runs/{run_id}/stop", headers=AUTH)

            fresh = await (await cli.get(f"/v1/runs/{run_id}", headers=AUTH)).json()
            assert fresh["status"] == "stopping"
            assert fresh["stop_wedged"] is False
            assert fresh["stopping_for_seconds"] >= 0

            await asyncio.sleep(0.1)
            wedged = await (await cli.get(f"/v1/runs/{run_id}", headers=AUTH)).json()
            assert wedged["status"] == "stopping", "no forced cancel"
            assert wedged["stop_wedged"] is True
            assert wedged["stopping_for_seconds"] >= 0.05
            # Still real work: it holds its slot until the thread returns.
            assert adapter.active_agent_work_count() == 1
            # The derived fields are computed at read time, never stored.
            assert "stop_wedged" not in adapter._run_statuses[run_id]

            release.set()
            await asyncio.wait_for(body_task, timeout=10)

    assert adapter._run_statuses[run_id]["status"] == "cancelled"
    assert adapter.active_agent_work_count() == 0


@pytest.mark.asyncio
async def test_sessions_stream_run_spends_exactly_one_concurrency_slot(adapter, session_db):
    """Registering the task must not double-count against max_concurrent_runs.

    _run_agent already counts this path via _inflight_agent_runs; the task
    registration exists for stop reachability, not for accounting.
    """
    session_id = session_db.create_session("one-slot", "api_server")
    agent, ready, _interrupted, release = _slow_agent(honours_interrupt=False)

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            resp = await _start_stream(cli, session_id)
            body_task = asyncio.ensure_future(resp.text())
            await _wait_for(lambda: ready.is_set())
            assert adapter.active_agent_work_count() == 1
            release.set()
            await asyncio.wait_for(body_task, timeout=10)

    assert adapter.active_agent_work_count() == 0


# ---------------------------------------------------------------------------
# 5 — the post-success race, on POST /v1/runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_that_loses_the_race_keeps_the_completed_output(adapter):
    """A stop the turn never honoured must not throw the answer away.

    _stopping_run_ids is only read after the executor returns, so this branch
    catches both a stop that landed microseconds after a successful turn and a
    stop the agent never noticed. Either way the session transcript has already
    persisted a complete answer; dropping `output`/`usage` from the run record
    left a client showing "cancelled" and nothing else, then a full answer on
    reload.

    The status stays "cancelled": the agent's own `interrupted` flag is False
    for both cases, so nothing can distinguish them, and flipping to
    "completed" would contradict
    test_stop_keeps_uncooperative_executor_tracked_until_exit. `interrupted` is
    published instead so a client can tell a turn that was genuinely cut short
    from one that ran to the end.
    """
    agent, ready, _interrupted, release = _slow_agent(honours_interrupt=False)

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            resp = await cli.post("/v1/runs", json={"input": "hello"}, headers=AUTH)
            run_id = (await resp.json())["run_id"]
            await _wait_for(lambda: ready.is_set())

            assert (await cli.post(f"/v1/runs/{run_id}/stop", headers=AUTH)).status == 200
            release.set()

            await _wait_for(lambda: run_id not in adapter._active_run_tasks)

    record = adapter._run_statuses[run_id]
    assert record["status"] == "cancelled"
    assert record["interrupted"] is False, "the agent never honoured the stop"
    assert record["output"] == "full answer"
    assert record["usage"]["total_tokens"] == 8


@pytest.mark.asyncio
async def test_stop_the_agent_honoured_reports_interrupted(adapter):
    agent, ready, _interrupted, _release = _slow_agent()

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            resp = await cli.post("/v1/runs", json={"input": "hello"}, headers=AUTH)
            run_id = (await resp.json())["run_id"]
            await _wait_for(lambda: ready.is_set())

            assert (await cli.post(f"/v1/runs/{run_id}/stop", headers=AUTH)).status == 200
            await _wait_for(lambda: run_id not in adapter._active_run_tasks)

    record = adapter._run_statuses[run_id]
    assert record["status"] == "cancelled"
    assert record["interrupted"] is True
    assert record["output"] == "partial answer"
