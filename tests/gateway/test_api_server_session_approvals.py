"""Command approvals on the sessions chat stream.

Approvals used to reach clients only through SSE ``/v1/runs`` or the
tui_gateway WebSocket. ``/api/sessions/{id}/chat/stream`` carried *clarify*
interactions but never registered an approval notify callback, so any client
on that transport blocked the agent for the full approval timeout with no
prompt and no way to answer. These tests pin the fix:

* the approval is published on the sessions stream as a ``clarify`` event with
  ``kind: "approval"`` and the full contract field set,
* ``choices`` tracks ``_approval_event_choices`` across smart-denied /
  tirith-flagged / normal cases,
* a client disconnect no longer orphans the approval — it stays listed and
  resolvable,
* expiry still reaps the record, and
* ``GET /v1/approvals/pending`` lets a reloaded client catch up, scoped to its
  own ``/p/<profile>/``.
"""

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import hermes_state
from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    _build_approval_record,
    _public_approval_record,
)
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


def _app(adapter: APIServerAdapter, *, multiplex: bool = False) -> web.Application:
    middlewares = []
    if multiplex:
        class _Runner:
            config = GatewayConfig(multiplex_profiles=True)

        adapter.gateway_runner = _Runner()
        middlewares.append(adapter._make_profile_prefix_middleware())
    app = web.Application(middlewares=middlewares)
    for method, path, handler in (
        ("POST", "/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream),
        ("POST", "/v1/runs/{run_id}/approval", adapter._handle_run_approval),
        ("GET", "/v1/approvals/pending", adapter._handle_list_pending_approvals),
    ):
        app.router.add_route(method, path, handler)
        app.router.add_route(method, f"/p/{{profile}}{path}", handler)
    return app


def _blocking_approval_agent(approval_data, *, seen: dict):
    """Mock agent whose turn blocks on a real gateway approval.

    ``run_conversation`` resolves the notify callback exactly the way
    ``check_all_command_guards`` does — ``_gateway_notify_cbs[session_key]``
    where ``session_key`` comes from ``get_current_session_key()``. That is the
    load-bearing part: if the adapter registers under a different key the
    lookup misses and the guard silently takes its dead fallback branch.
    """
    agent = MagicMock()

    def _run_conversation(**_kwargs):
        session_key = approval_mod.get_current_session_key()
        seen["session_key"] = session_key
        with approval_mod._lock:
            notify_cb = approval_mod._gateway_notify_cbs.get(session_key)
        seen["notify_cb"] = notify_cb
        if notify_cb is None:
            return {"final_response": "no approval callback registered"}
        seen["decision"] = approval_mod._await_gateway_decision(
            session_key, notify_cb, dict(approval_data), surface="gateway"
        )
        return {"final_response": "done"}

    agent.run_conversation.side_effect = _run_conversation
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_id = None
    return agent


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


async def _wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


_TERMINAL_APPROVAL = {
    "command": "cp payload /etc/systemd/system/x.service",
    "pattern_key": "system-config-write",
    "pattern_keys": ["system-config-write"],
    "description": "copy/move file into system config path",
    "allow_permanent": True,
}


# ---------------------------------------------------------------------------
# 1 — the event, on the sessions chat stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_stream_emits_approval_as_clarify_event(adapter, session_db):
    session_id = session_db.create_session("approval-stream", "api_server")
    seen: dict = {}
    agent = _blocking_approval_agent(_TERMINAL_APPROVAL, seen=seen)

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream",
                json={"message": "install the unit file"},
                headers=AUTH,
            )
            assert resp.status == 200
            body_task = asyncio.ensure_future(resp.text())

            run_id = await _wait_for(
                lambda: next(iter(adapter._run_approval_requests), None)
            )
            approval = await _wait_for(
                lambda: (adapter._run_approval_requests.get(run_id) or [None])[0]
            )

            resolve = await cli.post(
                f"/v1/runs/{run_id}/approval", json={"choice": "once"}, headers=AUTH
            )
            assert resolve.status == 200, await resolve.text()
            body = await asyncio.wait_for(body_task, timeout=10)

    frames = _parse_sse(body)
    clarify = [payload for name, payload in frames if name == "clarify.request"]
    interaction = [payload for name, payload in frames if name == "interaction.request"]
    assert len(clarify) == 1, [name for name, _ in frames]
    # Same payload on both names; only the stream's own seq/ts stamps differ.
    assert len(interaction) == 1
    _envelope = {"seq", "ts"}
    assert {k: v for k, v in interaction[0].items() if k not in _envelope} == {
        k: v for k, v in clarify[0].items() if k not in _envelope
    }

    event = clarify[0]
    # Contract v1 §1 — every field the UI renders from.
    assert event["kind"] == "approval"
    assert event["tool_name"] == "approval"
    assert event["interaction_id"] == event["approval_id"]
    assert event["interaction_id"].startswith("approval_")
    assert event["run_id"] == run_id
    assert event["session_id"] == session_id
    assert event["message_id"].startswith("msg_")
    assert event["choices"] == ["once", "session", "always", "deny"]
    assert event["command"] == _TERMINAL_APPROVAL["command"]
    assert event["description"] == _TERMINAL_APPROVAL["description"]
    assert event["pattern_key"] == "system-config-write"
    assert event["pattern_keys"] == ["system-config-write"]
    assert event["allow_permanent"] is True
    # Absent, never false — the UI tests `=== true`.
    assert "smart_denied" not in event
    # Absolute deadline: expiry emits no event, so the client runs its own
    # countdown and must not assume the default 60s.
    assert event["expires_at"].endswith("Z")
    assert event["expires_at"] > "2020-01-01T00:00:00Z"
    # Server-internal bookkeeping never goes on the wire.
    assert not [key for key in event if key.startswith("_")]

    # Registered under the key the guard resolves, and actually answered.
    assert seen["session_key"] == session_id
    assert seen["notify_cb"] is not None
    assert seen["decision"] == {"resolved": True, "choice": "once", "reason": None}
    assert approval["approval_id"] == event["approval_id"]

    # Run-scoped state is released once the agent thread returns.
    await _wait_for(lambda: run_id not in adapter._run_approval_sessions)
    assert run_id not in adapter._run_approval_requests
    # …and the status record left behind is reapable rather than pinned at
    # waiting_for_approval forever.
    adapter._run_statuses[run_id]["updated_at"] -= adapter._RUN_STATUS_TTL + 1
    adapter._sweep_orphaned_runs_once(time.time())
    assert run_id not in adapter._run_statuses


@pytest.mark.asyncio
async def test_session_stream_without_notify_would_have_hung(adapter, session_db):
    """Guard-rail for the original bug: prove the callback is what saves us."""
    session_id = session_db.create_session("no-callback", "api_server")
    seen: dict = {}
    agent = _blocking_approval_agent(_TERMINAL_APPROVAL, seen=seen)

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            with patch.object(adapter, "_run_agent", wraps=adapter._run_agent) as spy:
                resp = await cli.post(
                    f"/api/sessions/{session_id}/chat/stream",
                    json={"message": "hi"},
                    headers=AUTH,
                )
                run_id = await _wait_for(
                    lambda: next(iter(adapter._run_approval_requests), None)
                )
                await cli.post(
                    f"/v1/runs/{run_id}/approval", json={"choice": "deny"}, headers=AUTH
                )
                await asyncio.wait_for(resp.text(), timeout=10)

    kwargs = spy.call_args.kwargs
    assert kwargs["approval_notify"] is not None
    assert kwargs["approval_session_key"] == session_id
    assert kwargs["approval_cleanup"] is not None


# ---------------------------------------------------------------------------
# 2 — choices come from _approval_event_choices, verbatim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("guard_payload", "expected_choices", "expect_smart_denied"),
    [
        pytest.param(
            {"allow_permanent": True},
            ["once", "session", "always", "deny"],
            False,
            id="normal-dangerous-command",
        ),
        pytest.param(
            # tirith finding present → has_tirith forces allow_permanent False
            {"allow_permanent": False},
            ["once", "session", "deny"],
            False,
            id="tirith-flagged",
        ),
        pytest.param(
            {"allow_permanent": False, "smart_denied": True},
            ["once", "deny"],
            True,
            id="smart-denied-owner-override",
        ),
        pytest.param(
            # allow_permanent missing counts as true (defensive read)
            {},
            ["once", "session", "always", "deny"],
            False,
            id="allow-permanent-absent",
        ),
    ],
)
def test_approval_record_choices_track_guard_capabilities(
    guard_payload, expected_choices, expect_smart_denied
):
    record = _build_approval_record(
        {"command": "rm -rf /tmp/x", "description": "recursive delete", **guard_payload},
        approval_id="approval_1",
        session_id="sess",
        run_id="run_1",
        message_id="msg_1",
    )
    public = _public_approval_record(record)
    assert public["choices"] == expected_choices
    assert ("smart_denied" in public) is expect_smart_denied


_FAKE_GHP = "ghp_" + "X" * 36


def test_approval_record_redacts_command_for_display():
    record = _build_approval_record(
        {"command": f"curl -H 'Authorization: token {_FAKE_GHP}' https://api.github.com"},
        approval_id="approval_1",
        session_id="sess",
        run_id="run_1",
        message_id="msg_1",
    )
    assert _FAKE_GHP not in record["command"]


@pytest.mark.asyncio
async def test_session_stream_never_emits_a_raw_credential(adapter, session_db):
    """The sessions stream is a new egress point for issue #48456."""
    session_id = session_db.create_session("redaction", "api_server")
    seen: dict = {}
    agent = _blocking_approval_agent(
        {
            "command": f"curl -H 'Authorization: token {_FAKE_GHP}' https://api.github.com",
            "pattern_key": "network-exfil",
            "pattern_keys": ["network-exfil"],
            "description": "credentialed outbound request",
            "allow_permanent": True,
        },
        seen=seen,
    )

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream",
                json={"message": "fetch it"},
                headers=AUTH,
            )
            body_task = asyncio.ensure_future(resp.text())
            run_id = await _wait_for(
                lambda: next(iter(adapter._run_approval_requests), None)
            )
            listing = await (await cli.get("/v1/approvals/pending", headers=AUTH)).json()
            await cli.post(
                f"/v1/runs/{run_id}/approval", json={"choice": "deny"}, headers=AUTH
            )
            body = await asyncio.wait_for(body_task, timeout=10)

    # Both egress points — the stream frame and the catch-up listing.
    assert _FAKE_GHP not in body
    assert _FAKE_GHP not in json.dumps(listing)
    assert "api.github.com" in listing["approvals"][0]["command"]


def test_approval_record_expires_at_uses_configured_timeout():
    with patch("tools.approval._get_approval_timeout", return_value=180):
        record = _build_approval_record(
            {"command": "x"},
            approval_id="approval_1",
            session_id="sess",
            run_id="run_1",
            message_id="msg_1",
            now=1765432100.0,
        )
    assert record["_expires_at_ts"] == pytest.approx(1765432280.0)
    assert record["expires_at"] == "2025-12-11T05:51:20Z"


# ---------------------------------------------------------------------------
# 3 — a disconnect must not orphan the approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_stream_disconnect_keeps_approval_resolvable(adapter, session_db, monkeypatch):
    """A browser refresh is not an answer.

    The SSE writer's teardown must leave the approval alone: the agent thread
    is still blocked on it, and the user must be able to come back and decide.
    """
    monkeypatch.setattr(
        "gateway.platforms.api_server.CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS", 0.05
    )
    session_id = session_db.create_session("disconnect", "api_server")
    seen: dict = {}
    agent = _blocking_approval_agent(_TERMINAL_APPROVAL, seen=seen)

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch.object(adapter, "_create_agent", return_value=agent):
            with patch("tools.approval._get_approval_timeout", return_value=120):
                resp = await cli.post(
                    f"/api/sessions/{session_id}/chat/stream",
                    json={"message": "install the unit file"},
                    headers=AUTH,
                )
                run_id = await _wait_for(
                    lambda: next(iter(adapter._run_approval_requests), None)
                )

                # Hard client disconnect. The writer notices on its next
                # keepalive and cancels the streaming task.
                resp.close()
                await _wait_for(
                    lambda: (None, session_id) not in adapter._clarify_streams
                )

                # Approval state survived the teardown …
                assert adapter._run_approval_sessions[run_id] == session_id
                assert len(adapter._run_approval_requests[run_id]) == 1

                # … a reloaded client can still discover it …
                listing = await cli.get("/v1/approvals/pending", headers=AUTH)
                assert listing.status == 200
                pending = (await listing.json())["approvals"]
                assert [item["run_id"] for item in pending] == [run_id]

                # … and answer it.
                resolve = await cli.post(
                    f"/v1/runs/{run_id}/approval", json={"choice": "once"}, headers=AUTH
                )
                assert resolve.status == 200, await resolve.text()
                await _wait_for(lambda: seen.get("decision"))

    assert seen["decision"]["choice"] == "once"
    await _wait_for(lambda: run_id not in adapter._run_approval_sessions)


@pytest.mark.asyncio
async def test_stream_teardown_leaves_a_sibling_approval_answerable(adapter, session_db):
    """Two streams on one session share an approval key.

    Tearing one down must not signal the other's queued approval — that would
    resolve it as an un-answerable timeout, which is the failure this whole
    change exists to remove.
    """
    session_id = session_db.create_session("siblings", "api_server")
    seen: dict = {}
    blocking = _blocking_approval_agent(_TERMINAL_APPROVAL, seen=seen)
    quick = MagicMock()
    quick.run_conversation.return_value = {"final_response": "nothing dangerous"}
    quick.session_prompt_tokens = quick.session_completion_tokens = 0
    quick.session_total_tokens = 0
    quick.session_id = None

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch("tools.approval._get_approval_timeout", return_value=120):
            with patch.object(adapter, "_create_agent", return_value=blocking):
                blocked = await cli.post(
                    f"/api/sessions/{session_id}/chat/stream",
                    json={"message": "dangerous"},
                    headers=AUTH,
                )
                blocked_body = asyncio.ensure_future(blocked.text())
                run_id = await _wait_for(
                    lambda: next(iter(adapter._run_approval_requests), None)
                )

            # A second turn on the same session starts, finishes, and tears
            # down — taking the shared notify registration with it.
            with patch.object(adapter, "_create_agent", return_value=quick):
                sibling = await cli.post(
                    f"/api/sessions/{session_id}/chat/stream",
                    json={"message": "harmless"},
                    headers=AUTH,
                )
                await asyncio.wait_for(sibling.text(), timeout=10)

            # The first turn's approval is untouched and still answerable.
            assert seen.get("decision") is None
            assert len(adapter._run_approval_requests[run_id]) == 1
            resolve = await cli.post(
                f"/v1/runs/{run_id}/approval", json={"choice": "once"}, headers=AUTH
            )
            assert resolve.status == 200, await resolve.text()
            await asyncio.wait_for(blocked_body, timeout=10)

    assert seen["decision"] == {"resolved": True, "choice": "once", "reason": None}


@pytest.mark.asyncio
async def test_run_events_disconnect_keeps_stream_for_reconnect(adapter):
    """``/v1/runs`` reconnect used to 404 — the queue was popped on disconnect."""
    run_id = "run_disconnect"
    queue: "asyncio.Queue" = asyncio.Queue()
    adapter._run_streams[run_id] = queue
    adapter._run_streams_created[run_id] = time.time()

    app = web.Application()
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)

    async with TestClient(TestServer(app)) as cli:
        first = await cli.get(f"/v1/runs/{run_id}/events", headers=AUTH)
        assert first.status == 200
        first.close()
        await _wait_for(lambda: run_id not in adapter._run_stream_subscribers)

        # Queue and TTL stamp both survive …
        assert adapter._run_streams[run_id] is queue
        assert run_id in adapter._run_streams_created

        # … so a reconnect resumes instead of 404ing, and drains what was
        # buffered while nobody was listening.
        await queue.put({"event": "approval.request", "run_id": run_id})
        await queue.put(None)
        second = await cli.get(f"/v1/runs/{run_id}/events", headers=AUTH)
        assert second.status == 200
        assert "approval.request" in await second.text()

    # Still bounded: with no subscriber, the orphan sweep reaps the transport
    # once the stream TTL has elapsed, whether or not the client ever returns.
    adapter._run_streams_created[run_id] -= adapter._RUN_STREAM_TTL + 1
    adapter._sweep_orphaned_runs_once(time.time())
    assert run_id not in adapter._run_streams
    assert run_id not in adapter._run_streams_created


@pytest.mark.asyncio
async def test_reconnect_to_a_finished_run_closes_instead_of_hanging(adapter, monkeypatch):
    """Keeping the queue must not create an immortal idle connection.

    A client can now reconnect to a run whose end-of-stream sentinel an
    earlier reader already consumed. With nothing left to send, the reader has
    to close rather than sit on keepalives forever.
    """
    monkeypatch.setattr(
        "gateway.platforms.api_server.RUN_EVENTS_SSE_KEEPALIVE_SECONDS", 0.02
    )
    run_id = "run_finished"
    adapter._run_streams[run_id] = asyncio.Queue()
    adapter._run_streams_created[run_id] = time.time()
    adapter._run_statuses[run_id] = {"run_id": run_id, "status": "completed"}

    app = web.Application()
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get(f"/v1/runs/{run_id}/events", headers=AUTH)
        body = await asyncio.wait_for(resp.text(), timeout=5)

    assert "stream closed" in body


@pytest.mark.asyncio
async def test_reconnect_to_a_live_run_stays_open(adapter, monkeypatch):
    """…but a run that is still blocked on an approval must not be closed."""
    monkeypatch.setattr(
        "gateway.platforms.api_server.RUN_EVENTS_SSE_KEEPALIVE_SECONDS", 0.02
    )
    run_id = "run_waiting"
    queue: "asyncio.Queue" = asyncio.Queue()
    adapter._run_streams[run_id] = queue
    adapter._run_streams_created[run_id] = time.time()
    adapter._run_statuses[run_id] = {"run_id": run_id, "status": "waiting_for_approval"}

    app = web.Application()
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get(f"/v1/runs/{run_id}/events", headers=AUTH)
        # Several idle ticks pass, then a late approval still gets delivered.
        await asyncio.sleep(0.15)
        await queue.put({"event": "approval.request", "run_id": run_id})
        await queue.put(None)
        body = await asyncio.wait_for(resp.text(), timeout=5)

    assert "approval.request" in body


# ---------------------------------------------------------------------------
# 4 — expiry still reaps
# ---------------------------------------------------------------------------


def _seed(adapter, run_id, *, approval_id, profile=None, expires_in=60.0, now=None):
    now = time.time() if now is None else now
    with patch("tools.approval._get_approval_timeout", return_value=expires_in):
        record = _build_approval_record(
            dict(_TERMINAL_APPROVAL),
            approval_id=approval_id,
            session_id=f"sess-{run_id}",
            run_id=run_id,
            message_id=f"msg-{run_id}",
            profile=profile,
            now=now,
        )
    adapter._run_approval_requests.setdefault(run_id, []).append(record)
    return record


def test_expired_approval_records_are_pruned(adapter):
    _seed(adapter, "run_live", approval_id="approval_live", expires_in=600)
    _seed(adapter, "run_dead", approval_id="approval_dead", expires_in=-1)
    _seed(adapter, "run_mixed", approval_id="approval_mixed_dead", expires_in=-1)
    _seed(adapter, "run_mixed", approval_id="approval_mixed_live", expires_in=600)

    adapter._prune_expired_approval_requests()

    assert "run_dead" not in adapter._run_approval_requests
    assert [r["approval_id"] for r in adapter._run_approval_requests["run_live"]] == [
        "approval_live"
    ]
    assert [r["approval_id"] for r in adapter._run_approval_requests["run_mixed"]] == [
        "approval_mixed_live"
    ]


def test_sweep_reaps_expired_approvals_without_any_client(adapter):
    """Nothing about reaping may depend on a client calling the list endpoint."""
    _seed(adapter, "run_abandoned", approval_id="approval_abandoned", expires_in=-1)
    adapter._sweep_orphaned_runs_once(time.time())
    assert adapter._run_approval_requests == {}


def test_sweep_reaps_a_dead_non_terminal_run_status(adapter):
    """Sessions-stream runs never register an executor task, so a status stuck
    at waiting_for_approval would otherwise never be reaped."""
    now = time.time()
    adapter._run_statuses["run_stuck"] = {
        "run_id": "run_stuck",
        "status": "waiting_for_approval",
        "updated_at": now - adapter._RUN_STATUS_TTL - 1,
    }
    adapter._run_statuses["run_blocked"] = {
        "run_id": "run_blocked",
        "status": "waiting_for_approval",
        "updated_at": now - adapter._RUN_STATUS_TTL - 1,
    }
    # …unless the approval really is still outstanding.
    adapter._run_approval_sessions["run_blocked"] = "sess-b"

    adapter._sweep_orphaned_runs_once(now)

    assert "run_stuck" not in adapter._run_statuses
    assert "run_blocked" in adapter._run_statuses


def test_records_without_a_deadline_are_left_alone(adapter):
    """Synthetic/legacy entries carry no ``_expires_at_ts``."""
    adapter._run_approval_requests["run_legacy"] = [
        {"approval_id": "approval_legacy", "run_id": "run_legacy"}
    ]
    adapter._prune_expired_approval_requests()
    assert "run_legacy" in adapter._run_approval_requests


@pytest.mark.asyncio
async def test_expired_approval_disappears_from_pending_list(adapter):
    _seed(adapter, "run_live", approval_id="approval_live", expires_in=600)
    _seed(adapter, "run_dead", approval_id="approval_dead", expires_in=-1)

    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await cli.get("/v1/approvals/pending", headers=AUTH)
        assert resp.status == 200
        approvals = (await resp.json())["approvals"]

    assert [item["approval_id"] for item in approvals] == ["approval_live"]


# ---------------------------------------------------------------------------
# 5 — GET /v1/approvals/pending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_list_returns_the_full_event_field_set(adapter):
    record = _seed(adapter, "run_a", approval_id="approval_a", expires_in=600)

    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await cli.get("/v1/approvals/pending", headers=AUTH)
        body = await resp.json()

    assert body["object"] == "hermes.approval.list"
    assert body["approvals"] == [_public_approval_record(record)]
    item = body["approvals"][0]
    for field in (
        "interaction_id",
        "kind",
        "run_id",
        "session_id",
        "choices",
        "command",
        "description",
        "pattern_key",
        "pattern_keys",
        "allow_permanent",
        "expires_at",
    ):
        assert field in item, field
    assert not [key for key in item if key.startswith("_")]


@pytest.mark.asyncio
async def test_pending_list_is_ordered_oldest_first(adapter):
    now = time.time()
    _seed(adapter, "run_new", approval_id="approval_new", expires_in=600, now=now + 5)
    _seed(adapter, "run_old", approval_id="approval_old", expires_in=600, now=now)

    async with TestClient(TestServer(_app(adapter))) as cli:
        body = await (await cli.get("/v1/approvals/pending", headers=AUTH)).json()

    assert [item["approval_id"] for item in body["approvals"]] == [
        "approval_old",
        "approval_new",
    ]


@pytest.mark.asyncio
async def test_pending_list_excludes_resolved_approvals(adapter):
    run_id = "run_resolved"
    _seed(adapter, run_id, approval_id="approval_resolved", expires_in=600)
    adapter._run_statuses[run_id] = {
        "run_id": run_id, "status": "waiting_for_approval", "session_id": "sess-x",
    }
    adapter._run_approval_sessions[run_id] = "sess-x"

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch("tools.approval.resolve_gateway_approval", return_value=1), \
             patch.object(adapter, "_persist_approval_receipt"):
            resolve = await cli.post(
                f"/v1/runs/{run_id}/approval", json={"choice": "once"}, headers=AUTH
            )
            assert resolve.status == 200
        body = await (await cli.get("/v1/approvals/pending", headers=AUTH)).json()

    assert body["approvals"] == []


@pytest.mark.asyncio
async def test_pending_list_requires_auth(adapter):
    _seed(adapter, "run_a", approval_id="approval_a", expires_in=600)
    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await cli.get("/v1/approvals/pending")
    assert resp.status == 401


@pytest.mark.asyncio
async def test_expired_head_does_not_mislabel_a_resolution(adapter):
    """The core drops a timed-out entry from its own queue; our record list
    must follow, or the FIFO head used to label the receipt is a dead one."""
    run_id = "run_stale_head"
    _seed(adapter, run_id, approval_id="approval_expired", expires_in=-1)
    _seed(adapter, run_id, approval_id="approval_live", expires_in=600)
    adapter._run_statuses[run_id] = {
        "run_id": run_id, "status": "waiting_for_approval", "session_id": "sess-x",
    }
    adapter._run_approval_sessions[run_id] = "sess-x"

    async with TestClient(TestServer(_app(adapter))) as cli:
        with patch("tools.approval.resolve_gateway_approval", return_value=1), \
             patch.object(adapter, "_persist_approval_receipt"):
            resp = await cli.post(
                f"/v1/runs/{run_id}/approval", json={"choice": "once"}, headers=AUTH
            )
            payload = await resp.json()

    assert payload["approval_id"] == "approval_live"


# ---------------------------------------------------------------------------
# 6 — profile scoping
# ---------------------------------------------------------------------------


def test_pending_route_is_registered_with_a_profile_mirror(adapter):
    paths = {path for _method, path, _handler in adapter._http_route_table()}
    assert "/v1/approvals/pending" in paths
    assert f"/p/{{profile}}/v1/approvals/pending" in {
        f"/p/{{profile}}{path}" for path in paths
    }


@pytest.mark.asyncio
async def test_pending_list_is_scoped_to_the_requesting_profile(adapter, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("default", None), ("coder", None)],
    )
    _seed(adapter, "run_default", approval_id="approval_default", profile=None, expires_in=600)
    _seed(adapter, "run_coder", approval_id="approval_coder", profile="coder", expires_in=600)

    app = _app(adapter, multiplex=True)
    async with TestClient(TestServer(app)) as cli:
        unprefixed = await (await cli.get("/v1/approvals/pending", headers=AUTH)).json()
        prefixed = await (
            await cli.get("/p/coder/v1/approvals/pending", headers=AUTH)
        ).json()
        unknown = await cli.get("/p/ghost/v1/approvals/pending", headers=AUTH)

    assert [i["approval_id"] for i in unprefixed["approvals"]] == ["approval_default"]
    assert [i["approval_id"] for i in prefixed["approvals"]] == ["approval_coder"]
    assert unknown.status == 404
