"""Focused tests for API server session-control endpoints."""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB


@pytest.fixture
def session_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        yield db
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


@pytest.fixture
def adapter(session_db):
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = session_db
    return adapter


@pytest.fixture
def auth_adapter(session_db):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-test"}))
    adapter._session_db = session_db
    return adapter


def _create_session_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
    app.router.add_get("/api/sessions", adapter._handle_list_sessions)
    app.router.add_post("/api/sessions", adapter._handle_create_session)
    app.router.add_get("/api/sessions/{session_id}", adapter._handle_get_session)
    app.router.add_patch("/api/sessions/{session_id}", adapter._handle_patch_session)
    app.router.add_delete("/api/sessions/{session_id}", adapter._handle_delete_session)
    app.router.add_get("/api/sessions/{session_id}/messages", adapter._handle_session_messages)
    app.router.add_post("/api/sessions/{session_id}/fork", adapter._handle_fork_session)
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post("/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream)
    return app


@pytest.mark.asyncio
async def test_capabilities_advertises_session_control_surface(adapter):
    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/v1/capabilities")
        assert resp.status == 200
        data = await resp.json()

    features = data["features"]
    assert features["session_resources"] is True
    assert features["session_chat"] is True
    assert features["session_chat_streaming"] is True
    assert features["session_fork"] is True
    assert features["run_steer"] is True
    assert features["admin_config_rw"] is False
    assert features["memory_write_api"] is False
    assert features["skills_api"] is True
    assert features["realtime_voice"] is False
    assert data["endpoints"]["sessions"] == {"method": "GET", "path": "/api/sessions"}
    assert data["endpoints"]["session_chat_stream"] == {
        "method": "POST",
        "path": "/api/sessions/{session_id}/chat/stream",
    }
    assert data["endpoints"]["run_steer"] == {
        "method": "POST",
        "path": "/v1/runs/{run_id}/steer",
    }


@pytest.mark.asyncio
async def test_session_messages_default_to_latest_bounded_page(adapter, session_db):
    session_id = session_db.create_session("bounded-messages", "api_server")
    session_db.replace_messages(
        session_id,
        [{"role": "user", "content": f"msg {i}"} for i in range(501)],
    )

    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get(f"/api/sessions/{session_id}/messages")
        assert resp.status == 200
        payload = await resp.json()

        explicit_resp = await cli.get(
            f"/api/sessions/{session_id}/messages?limit=2&offset=1"
        )
        assert explicit_resp.status == 200
        explicit = await explicit_resp.json()

    assert payload["pagination"] == {
        "limit": 500,
        "offset": 0,
        "order": "latest",
        "returned": 500,
    }
    assert payload["data"][0]["content"] == "msg 1"
    assert payload["data"][-1]["content"] == "msg 500"
    assert [message["content"] for message in explicit["data"]] == [
        "msg 1",
        "msg 2",
    ]


@pytest.mark.asyncio
async def test_forked_session_stays_listable_and_parent_survives_failed_fork(adapter, session_db):
    """A fork is created before the parent is ended (#11030) and carries the explicit branch
    marker, so it still shows in the default listing (the timestamp fallback no longer holds)."""
    session_db.create_session("parent", "api_server")
    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/sessions/parent/fork", json={"id": "child"})
        assert resp.status == 201
        listed = await (await cli.get("/api/sessions")).json()
        ids = {row["id"] for row in listed["data"]}
        assert {"parent", "child"} <= ids, ids

        session_db.create_session("solo", "api_server")
        with patch.object(session_db, "create_session", side_effect=RuntimeError("boom")):
            resp = await cli.post("/api/sessions/solo/fork", json={"id": "never"})
        assert resp.status >= 500
    assert session_db.get_session("solo")["end_reason"] is None

@pytest.mark.asyncio
async def test_run_agent_binds_api_session_context_for_tool_env(adapter, monkeypatch):
    """API-server request sessions should reach tools and terminal subprocess env."""
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-session")
    observed = {}

    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def __init__(self, session_id: str):
            self.session_id = session_id

        def run_conversation(self, user_message, conversation_history, task_id):
            from gateway.session_context import get_session_env
            from tools.environments.local import _make_run_env

            observed["task_id"] = task_id
            observed["context_session_id"] = get_session_env("HERMES_SESSION_ID")
            observed["context_platform"] = get_session_env("HERMES_SESSION_PLATFORM")
            observed["context_session_key"] = get_session_env("HERMES_SESSION_KEY")
            observed["child_session_id"] = _make_run_env({}).get("HERMES_SESSION_ID")
            return {"final_response": "ok"}

    def fake_create_agent(**kwargs):
        return FakeAgent(kwargs["session_id"])

    monkeypatch.setattr(adapter, "_create_agent", fake_create_agent)

    result, usage = await adapter._run_agent(
        user_message="hello",
        conversation_history=[],
        session_id="request-session",
        gateway_session_key="request-key",
    )

    assert result["session_id"] == "request-session"
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["total_tokens"] == 0
    assert "runtime" not in usage
    assert observed == {
        "task_id": "request-session",
        "context_session_id": "request-session",
        "context_platform": "api_server",
        "context_session_key": "request-key",
        "child_session_id": "request-session",
    }


@pytest.mark.asyncio
async def test_run_agent_registers_active_run_id_for_steering(adapter, monkeypatch):
    observed = {}

    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def __init__(self, session_id: str):
            self.session_id = session_id

        def steer(self, text: str) -> bool:
            observed["steer_text"] = text
            return True

        def run_conversation(self, user_message, conversation_history, task_id):
            observed["registered"] = adapter._active_run_agents.get("run_steer_test") is self
            observed["task_id"] = task_id
            return {"final_response": "ok"}

    def fake_create_agent(**kwargs):
        return FakeAgent(kwargs["session_id"])

    monkeypatch.setattr(adapter, "_create_agent", fake_create_agent)

    result, usage = await adapter._run_agent(
        user_message="hello",
        conversation_history=[],
        session_id="request-session",
        active_run_id="run_steer_test",
    )

    assert result["session_id"] == "request-session"
    assert usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert observed == {"registered": True, "task_id": "request-session"}
    assert "run_steer_test" not in adapter._active_run_agents


@pytest.mark.asyncio
async def test_session_chat_stream_disconnect_keeps_control_refs_until_executor_finishes(
    adapter, session_db
):
    """A disconnect must not drop the run's control refs before the executor finishes.

    FORK SEMANTICS, chosen deliberately over upstream's. This transport does NOT
    interrupt the agent when the client goes away: the sessions stream is
    session-backed, its turn persists to the transcript, and its run-events
    transport survives a disconnect so a client can reconnect. Killing the run
    while keeping its stream would let a client reconnect to the stream of a run
    that was already dead, and a browser reload must not make a live agent
    unstoppable. The run stays registered and is ended explicitly via
    POST /v1/runs/{run_id}/stop.

    This differs from /v1/chat/completions, which IS interrupted on disconnect
    (tests/gateway/test_sse_agent_cancel.py) — that transport is stateless, so an
    abandoned turn has nobody to deliver to and only burns tokens.

    What this test still guards, unchanged, is the part that mattered: the
    control refs outlive the disconnect, so the run remains reachable until the
    executor thread genuinely returns.
    """
    session_id = session_db.create_session("disconnect-stream-session", "api_server")
    run_started = threading.Event()
    interrupt_called = threading.Event()
    allow_finish = threading.Event()
    write_calls = {"count": 0}

    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def __init__(self, stream_delta_callback):
            self._stream_delta_callback = stream_delta_callback
            self.session_id = session_id

        def interrupt(self, _message=None):
            interrupt_called.set()

        def run_conversation(self, user_message, conversation_history, task_id):
            del user_message, conversation_history, task_id
            run_started.set()
            self._stream_delta_callback("hello")
            allow_finish.wait(timeout=5)
            return {"final_response": "done", "session_id": session_id}

    class DisconnectingStreamResponse:
        async def prepare(self, request):
            del request

        async def write(self, payload):
            del payload
            write_calls["count"] += 1
            if write_calls["count"] >= 3:
                raise ConnectionResetError("simulated client disconnect")

    request = MagicMock()
    request.headers = {}
    request.match_info = {"session_id": session_id}

    def _create_agent(**kwargs):
        return FakeAgent(kwargs["stream_delta_callback"])

    with patch.object(
        adapter,
        "_get_existing_session_or_404",
        return_value=({"id": session_id}, None),
    ), patch.object(
        adapter,
        "_read_json_body",
        return_value=({"message": "stream please"}, None),
    ), patch.object(
        adapter,
        "_create_agent",
        side_effect=_create_agent,
    ), patch(
        "gateway.platforms.api_server.web.StreamResponse",
        return_value=DisconnectingStreamResponse(),
    ):
        handler_task = asyncio.create_task(adapter._handle_session_chat_stream(request))

        for _ in range(60):
            if run_started.is_set():
                break
            await asyncio.sleep(0.05)

        assert run_started.is_set()
        run_id = next(iter(adapter._run_statuses))

        # The agent is deliberately NOT interrupted by the disconnect (see docstring).
        assert not interrupt_called.is_set(), "disconnect must not kill a session-stream turn"
        assert run_id in adapter._active_run_agents
        # IS in _active_run_tasks: registering the task is what makes this run
        # reachable from POST /v1/runs/{run_id}/stop during the pre-agent window,
        # when _create_agent has not returned yet. The double-count that entry
        # would otherwise cause in active_agent_work_count is avoided by passing
        # count_inflight=False to _run_agent, not by leaving the task unregistered.
        assert run_id in adapter._active_run_tasks
        assert not handler_task.done()

        allow_finish.set()
        await handler_task

    assert run_id not in adapter._active_run_agents


@pytest.mark.asyncio
async def test_session_chat_stream_run_completed_carries_turn_transcript(adapter, session_db):
    """run.completed must include the full interleaved turn transcript so a
    client that lost intermediate (pre-tool-call) assistant text from the live
    delta stream can reconcile without a separate /messages fetch. Refs #34703.
    """
    import json as _json

    session_id = session_db.create_session("transcript-session", "api_server")

    async def fake_run(**kwargs):
        # Stream the intermediate planning text the way a real turn would.
        kwargs["stream_delta_callback"]("Let me search for that:")
        kwargs["stream_delta_callback"]("Here is the summary.")
        result = {
            "final_response": "Here is the summary.",
            "session_id": session_id,
            "messages": [
                {"role": "user", "content": "search then summarize"},
                {
                    "role": "assistant",
                    "content": "Let me search for that:",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "web_search", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "content": "results", "tool_call_id": "call_1", "tool_name": "web_search"},
                {"role": "assistant", "content": "Here is the summary."},
            ],
        }
        return result, {"total_tokens": 6}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream",
                json={"message": "search then summarize"},
            )
            assert resp.status == 200
            body = await resp.text()

    # Pull the run.completed event payload out of the SSE body.
    run_completed_payload = None
    for block in body.split("\n\n"):
        if "event: run.completed" in block:
            for line in block.splitlines():
                if line.startswith("data: "):
                    run_completed_payload = _json.loads(line[len("data: "):])
            break
    assert run_completed_payload is not None, body
    messages = run_completed_payload.get("messages")
    assert isinstance(messages, list) and messages, run_completed_payload

    # The colon-ended intermediate text that preceded the tool call must be present.
    contents = [m.get("content") for m in messages]
    assert "Let me search for that:" in contents
    assert "Here is the summary." in contents
    # No prior-turn user message should leak into the per-turn slice.
    assert all(m.get("role") in ("assistant", "tool") for m in messages)
    # The tool call is preserved alongside the intermediate text.
    assert any(m.get("tool_calls") for m in messages)


# ---------------------------------------------------------------------------
# Session-persisted model threading + provider-auth failure surfacing
# (salvaged from PR #57947 by @FvanW and PR #59941 by @kaishi00)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_chat_resolves_stored_model_route_alias(session_db, monkeypatch):
    """A session-persisted model that matches a model_routes alias must go
    through the route path (so route provider/credentials apply) and NOT be
    passed as a raw session_model (idea from PR #59941 by @kaishi00)."""
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={"model_routes": {"alias": {"model": "route/model", "provider": "openrouter"}}},
        )
    )
    adapter._session_db = session_db
    session_id = session_db.create_session("route-pinned-session", "api_server", model="alias")

    mock_run = AsyncMock(return_value=({"final_response": "ok", "session_id": session_id}, {"total_tokens": 1}))
    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", mock_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "hi"},
            )
            assert resp.status == 200

    _, kwargs = mock_run.call_args
    assert kwargs["route"] == {"model": "route/model", "provider": "openrouter"}
    assert kwargs["session_model"] is None


@pytest.mark.asyncio
async def test_session_chat_treats_pre_existing_poisoned_row_as_no_model(session_db):
    """A session row created before the alias-leak fix may still have the
    virtual model alias (e.g. "hermes-agent") persisted literally as its
    model. Reading that back must NOT thread it through as a raw
    session_model override — it must fall through to the global default,
    exactly like a row that never had a model at all (#session-model-
    alias-leak)."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = session_db
    session_id = session_db.create_session(
        "poisoned-session", "api_server", model=adapter._model_name
    )

    mock_run = AsyncMock(return_value=({"final_response": "ok", "session_id": session_id}, {"total_tokens": 1}))
    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", mock_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "hi"},
            )
            assert resp.status == 200

    _, kwargs = mock_run.call_args
    assert kwargs["session_model"] is None


@pytest.mark.asyncio
async def test_session_chat_stream_treats_pre_existing_poisoned_row_as_no_model(session_db):
    """Streaming twin of the above: the SSE chat path must apply the same
    guard against a pre-existing poisoned session row."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = session_db
    session_id = session_db.create_session(
        "poisoned-stream-session", "api_server", model=adapter._model_name
    )

    async def fake_run(**kwargs):
        return {"final_response": "ok", "session_id": session_id}, {"total_tokens": 1}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run) as mock_run:
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream",
                json={"message": "hi"},
            )
            assert resp.status == 200
            # Drain the SSE body: the 200 lands before the streaming task
            # invokes _run_agent, so asserting on call_args without reading
            # the body races the handler (flaked on loaded CI runners).
            await resp.text()

    _, kwargs = mock_run.call_args
    assert kwargs["session_model"] is None


def _register_session_model_route(app, adapter):
    app.router.add_post("/api/sessions/{session_id}/model", adapter._handle_session_model_lock)


def _patch_api_server_runtime(monkeypatch):
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_key": "sk-global",
            "base_url": "https://openrouter.example/v1",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "global/model")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr(
        "gateway.run.GatewayRunner._load_reasoning_config",
        staticmethod(lambda model="": {}),
    )
    monkeypatch.setattr(
        "gateway.run.GatewayRunner._load_fallback_model",
        staticmethod(lambda: None),
    )
    monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 90)
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *_: set())
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider",
        lambda provider: {
            "provider": provider,
            "api_key": f"sk-{provider}",
            "base_url": f"https://{provider}.example/v1",
            "api_mode": "chat_completions",
        },
    )


@pytest.mark.asyncio
async def test_create_session_respects_browser_source_and_model_lock(adapter, session_db):
    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/sessions",
            json={
                "id": "browser-lock-session",
                "source": "hermes_browser",
                "provider": "nous",
                "model": "x-ai/grok-4.5",
                "require_model_lock": True,
                "title": "Browser lock",
                "system_prompt": "browser prompt",
            },
        )
        assert resp.status == 201, await resp.text()
        payload = await resp.json()

    assert payload["session"]["source"] == "hermes_browser"
    assert payload["session"]["model"] == "x-ai/grok-4.5"
    row = session_db.get_session("browser-lock-session")
    assert row["source"] == "hermes_browser"
    assert row["model"] == "x-ai/grok-4.5"
    import json as _json
    model_config = row.get("model_config")
    if isinstance(model_config, str):
        model_config = _json.loads(model_config)
    assert model_config["browser_model_lock"]["provider"] == "nous"
    assert model_config["browser_model_lock"]["model"] == "x-ai/grok-4.5"
    assert model_config["browser_model_lock"]["confirmed"] is True


@pytest.mark.asyncio
async def test_session_model_lock_endpoint_then_chat_reuses_persisted_lock_and_provider_credentials(
    adapter,
    session_db,
    monkeypatch,
):
    session_id = session_db.create_session(
        "endpoint-lock-chat",
        "api_server",
        model="gpt-5.5",
        system_prompt="Conversation started:\nModel: gpt-5.5\nProvider: openai-codex\n",
    )
    captured = {}

    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.session_id = kwargs["session_id"]
            self.provider = kwargs.get("provider") or ""
            self.model = kwargs.get("model") or ""

        def run_conversation(self, user_message, conversation_history, task_id):
            return {"final_response": "locked", "session_id": self.session_id}

    _patch_api_server_runtime(monkeypatch)
    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    monkeypatch.setattr(
        adapter,
        "_session_model_override_for",
        lambda *_: {
            "model": "session/override-model",
            "provider": "openai-codex",
            "api_key": "sk-session-override",
            "base_url": "https://override.example/v1",
            "api_mode": "codex_responses",
        },
    )

    app = _create_session_app(adapter)
    _register_session_model_route(app, adapter)
    with patch.object(adapter, "_resolve_route", return_value=None):
        async with TestClient(TestServer(app)) as cli:
            lock_resp = await cli.post(
                f"/api/sessions/{session_id}/model",
                json={
                    "provider": "nous",
                    "model": "x-ai/grok-4.5",
                    "require_model_lock": True,
                },
            )
            assert lock_resp.status == 200, await lock_resp.text()

            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "use the stored lock"},
            )
            assert resp.status == 200, await resp.text()
            payload = await resp.json()

    assert captured["provider"] == "nous"
    assert captured["model"] == "x-ai/grok-4.5"
    assert captured["api_key"] == "sk-nous"
    assert captured["base_url"] == "https://nous.example/v1"
    assert payload["runtime"]["provider"] == "nous"
    assert payload["runtime"]["model"] == "x-ai/grok-4.5"
    assert payload["runtime"]["requested"] == {
        "provider": "nous",
        "model": "x-ai/grok-4.5",
    }
    assert payload["runtime"]["route_source"] == "session_model_lock"


@pytest.mark.asyncio
async def test_session_model_lock_endpoint_then_chat_stream_reuses_persisted_lock(
    adapter,
    session_db,
):
    session_id = session_db.create_session("endpoint-lock-stream", "api_server")
    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        kwargs["stream_delta_callback"]("hi")
        return (
            {
                "final_response": "hi",
                "session_id": session_id,
                "runtime": {
                    "provider": "nous",
                    "model": "x-ai/grok-4.5",
                    "requested": {"provider": "nous", "model": "x-ai/grok-4.5"},
                    "route_source": "session_model_lock",
                },
            },
            {
                "total_tokens": 1,
                "runtime": {
                    "provider": "nous",
                    "model": "x-ai/grok-4.5",
                    "requested": {"provider": "nous", "model": "x-ai/grok-4.5"},
                    "route_source": "session_model_lock",
                },
            },
        )

    app = _create_session_app(adapter)
    _register_session_model_route(app, adapter)
    with patch.object(adapter, "_resolve_route", return_value=None), patch.object(
        adapter,
        "_run_agent",
        side_effect=fake_run,
    ):
        async with TestClient(TestServer(app)) as cli:
            lock_resp = await cli.post(
                f"/api/sessions/{session_id}/model",
                json={
                    "provider": "nous",
                    "model": "x-ai/grok-4.5",
                    "require_model_lock": True,
                },
            )
            assert lock_resp.status == 200, await lock_resp.text()

            resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream",
                json={"message": "stream with stored lock"},
            )
            assert resp.status == 200, await resp.text()
            body = await resp.text()

    assert captured["route"] == {"provider": "nous", "model": "x-ai/grok-4.5"}
    assert captured["requested_runtime"]["provider"] == "nous"
    assert captured["requested_runtime"]["model"] == "x-ai/grok-4.5"
    assert captured["route_source"] == "session_model_lock"
    assert "x-ai/grok-4.5" in body


@pytest.mark.asyncio
async def test_run_agent_reports_actual_agent_runtime_not_requested_metadata(adapter, monkeypatch):
    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def __init__(self):
            self.session_id = "runtime-session"
            self.provider = "actual-provider"
            self.model = "actual-model"
            self._hermes_api_runtime = {
                "provider": "requested-provider",
                "model": "requested-model",
                "route_source": "raw_request",
            }

        def run_conversation(self, user_message, conversation_history, task_id):
            return {"final_response": "ok", "session_id": self.session_id}

    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: FakeAgent())

    result, usage = await adapter._run_agent(
        user_message="hello",
        conversation_history=[],
        session_id="runtime-session",
        route={"provider": "requested-provider", "model": "requested-model"},
        requested_runtime={
            "provider": "requested-provider",
            "model": "requested-model",
        },
        route_source="session_model_lock",
    )

    assert result["runtime"]["provider"] == "actual-provider"
    assert result["runtime"]["model"] == "actual-model"
    assert result["runtime"]["requested"] == {
        "provider": "requested-provider",
        "model": "requested-model",
    }
    assert usage["runtime"]["provider"] == "actual-provider"
    assert usage["runtime"]["model"] == "actual-model"


@pytest.mark.asyncio
async def test_confirmed_runtime_lock_rejects_actual_runtime_mismatch(adapter, monkeypatch):
    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0
        session_id = "mismatch-session"
        provider = "fallback-provider"
        model = "fallback-model"

        def run_conversation(self, user_message, conversation_history, task_id):
            return {"final_response": "wrong runtime", "session_id": self.session_id}

    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: FakeAgent())

    with pytest.raises(RuntimeError, match="confirmed model lock runtime mismatch"):
        await adapter._run_agent(
            user_message="hello",
            conversation_history=[],
            session_id="mismatch-session",
            route={"provider": "nous", "model": "x-ai/grok-4.5"},
            requested_runtime={"provider": "nous", "model": "x-ai/grok-4.5"},
            route_source="session_model_lock",
            confirmed_runtime_lock=True,
        )


def test_confirmed_runtime_lock_disables_global_fallback_model(adapter, monkeypatch):
    _patch_api_server_runtime(monkeypatch)
    monkeypatch.setattr(
        "gateway.run.GatewayRunner._load_fallback_model",
        staticmethod(lambda: "openrouter/fallback-model"),
    )
    captured = {}

    class FakeAgent:
        provider = "nous"
        model = "x-ai/grok-4.5"

        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)

    adapter._create_agent(
        session_id="locked-session",
        route={"provider": "nous", "model": "x-ai/grok-4.5"},
        confirmed_runtime_lock=True,
    )

    assert captured["fallback_model"] is None


@pytest.mark.asyncio
async def test_unconfirmed_request_does_not_replace_confirmed_session_lock(adapter, session_db):
    session_id = session_db.create_session("one-off-override", "api_server")
    session_db.update_session_runtime_lock(
        session_id,
        provider="nous",
        model="x-ai/grok-4.5",
        route_source="raw_request",
        confirmed=True,
    )
    mock_run = AsyncMock(
        return_value=(
            {
                "final_response": "ok",
                "session_id": session_id,
                "runtime": {"provider": "openrouter", "model": "anthropic/claude-sonnet"},
            },
            {"total_tokens": 1},
        )
    )
    app = _create_session_app(adapter)
    with patch.object(adapter, "_resolve_route", return_value=None), patch.object(
        adapter,
        "_run_agent",
        mock_run,
    ):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={
                    "message": "one turn only",
                    "provider": "openrouter",
                    "model": "anthropic/claude-sonnet",
                },
            )
            assert resp.status == 200, await resp.text()

    import json as _json

    row = session_db.get_session(session_id)
    config = row["model_config"]
    if isinstance(config, str):
        config = _json.loads(config)
    assert config["browser_model_lock"]["provider"] == "nous"
    assert config["browser_model_lock"]["model"] == "x-ai/grok-4.5"
    assert config["browser_model_lock"]["confirmed"] is True


@pytest.mark.asyncio
async def test_require_model_lock_hard_fails_when_global_default_would_be_used(adapter, session_db, monkeypatch):
    session_id = session_db.create_session("lock-fail-session", "api_server")
    monkeypatch.setattr(adapter, "_model_name", "gpt-5.5")
    app = _create_session_app(adapter)
    with patch.object(adapter, "_resolve_route", return_value=None), patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        async with TestClient(TestServer(app)) as cli:
            # empty model + require_model_lock must not silently fall through
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={
                    "message": "hello",
                    "provider": "nous",
                    "model": "",
                    "require_model_lock": True,
                },
            )
            assert resp.status in (400, 409), await resp.text()
            body = await resp.json()
            assert body["error"]["code"] in {"model_lock_unavailable", "invalid_model_lock", "missing_model"}
    mock_run.assert_not_called()


_CHAT_REPLY = ({"final_response": "ok"}, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})


@pytest.mark.asyncio
@pytest.mark.parametrize("body, expected", [
    ({"message": "hello", "author": {"id": " bot:dixie ", "name": "dixie", "is_bot": 1, "role": "admin"}},
     {"id": "bot:dixie", "name": "dixie", "is_bot": True}),
    ({"message": "hello"}, None),
    ({"message": "hello", "author": None}, None),
], ids=["author", "no author", "null author"])
async def test_session_chat_passes_normalized_author_to_run_agent(adapter, session_db, body, expected):
    """A body ``author`` reaches ``_run_agent`` normalized with unknown keys dropped; absent or null is None."""
    session_id = session_db.create_session("author-session", "api_server")
    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", AsyncMock(return_value=_CHAT_REPLY)) as mock_run:
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/api/sessions/{session_id}/chat", json=body)
            assert resp.status == 200, await resp.text()
    assert mock_run.call_args.kwargs["turn_author"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["/chat", "/chat/stream"])
@pytest.mark.parametrize("author", ["dixie", ["dixie"], 7])
async def test_session_chat_rejects_non_object_author(adapter, session_db, suffix, author):
    session_id = session_db.create_session("bad-author-session", "api_server")
    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}{suffix}", json={"message": "hello", "author": author})
            assert resp.status == 400, await resp.text()
            body = await resp.json()
    assert body["error"]["code"] == "invalid_author"
    assert body["error"]["message"] == "author must be an object"
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_run_agent_forwards_author_to_run_conversation_only_when_set(adapter, monkeypatch):
    """``turn_author`` reaches ``run_conversation`` when set; a human turn keeps today's call shape."""
    calls = []

    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0
        session_id = "author-run"

        def run_conversation(self, user_message, conversation_history, task_id, **kwargs):
            calls.append(kwargs)
            return {"final_response": "ok", "session_id": self.session_id}

    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: FakeAgent())
    author = {"id": "bot:dixie", "name": "dixie", "is_bot": True}
    await adapter._run_agent(
        user_message="hello", conversation_history=[], session_id="author-run", turn_author=author)
    await adapter._run_agent(user_message="hello", conversation_history=[], session_id="author-run")

    assert calls == [{"turn_author": author}, {}]


@pytest.mark.asyncio
async def test_patch_session_persists_pinned_and_archived(adapter, session_db):
    """PATCH must accept the durable pin/archive flags and round-trip them.

    These were rejected as unsupported fields, so every pin the desktop made
    400'd silently (the client swallows the error) and the pin only ever lived
    in that one app's localStorage. The auto-archive sweep reads
    `sessions.pinned` server-side, so an unpersisted pin does not protect the
    chat it was supposed to keep.
    """
    session_id = session_db.create_session("pin-session", "api_server")
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.patch(f"/api/sessions/{session_id}", json={"pinned": True})
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["session"]["pinned"] is True

        # The flag is durable, not just echoed back from the request body.
        assert bool(session_db.get_session(session_id)["pinned"]) is True

        resp = await cli.get(f"/api/sessions/{session_id}")
        assert (await resp.json())["session"]["pinned"] is True

        resp = await cli.patch(f"/api/sessions/{session_id}", json={"pinned": False})
        assert (await resp.json())["session"]["pinned"] is False
        assert bool(session_db.get_session(session_id)["pinned"]) is False

        resp = await cli.patch(f"/api/sessions/{session_id}", json={"archived": True})
        assert (await resp.json())["session"]["archived"] is True
        assert bool(session_db.get_session(session_id)["archived"]) is True


@pytest.mark.asyncio
async def test_patch_session_rejects_non_boolean_pinned(adapter, session_db):
    session_id = session_db.create_session("pin-type-session", "api_server")
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.patch(f"/api/sessions/{session_id}", json={"pinned": "yes"})
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["error"]["code"] == "invalid_session_field"


@pytest.mark.asyncio
async def test_patch_session_still_rejects_unknown_fields(adapter, session_db):
    session_id = session_db.create_session("unknown-field-session", "api_server")
    app = _create_session_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.patch(f"/api/sessions/{session_id}", json={"nonsense": 1})
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["error"]["code"] == "unsupported_session_field"


# ──────────────────────────────────────────────────────────────────────
# /goal post-turn continuation on the session chat surfaces (#230)
# ──────────────────────────────────────────────────────────────────────


def _goal_manager_stub(decisions, *, turns_used=1, max_turns=20):
    """Build a fake hermes_cli.goals.GoalManager class yielding *decisions*.

    ``decisions`` is a list consumed one entry per evaluate_after_turn call;
    the last entry repeats once exhausted so a capped loop keeps continuing.
    """
    from types import SimpleNamespace

    calls = {"eval": [], "init": []}
    state = SimpleNamespace(turns_used=turns_used, max_turns=max_turns)
    pending = list(decisions)

    class _Stub:
        def __init__(self, session_id, **kwargs):
            calls["init"].append(session_id)
            self.session_id = session_id
            self.state = state

        def is_active(self):
            return bool(pending)

        def evaluate_after_turn(self, last_response, **kwargs):
            calls["eval"].append((last_response, kwargs))
            return pending.pop(0) if len(pending) > 1 else pending[0]

    return _Stub, calls


def _sse_events(body: str):
    """Parse an SSE body into a list of (event_name, payload dict)."""
    import json as _json

    events = []
    for block in body.split("\n\n"):
        name = None
        payload = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                payload = _json.loads(line[len("data: "):])
        if name is not None:
            events.append((name, payload or {}))
    return events


@pytest.mark.asyncio
async def test_session_chat_omits_goal_block_and_skips_judge_when_no_goal(adapter, session_db):
    """The no-goal path must cost one state read and change no bytes on the wire."""
    session_id = session_db.create_session("goal-none", "api_server")
    stub, calls = _goal_manager_stub([])  # is_active() -> False

    async def fake_run(**kwargs):
        return {"final_response": "hi", "session_id": session_id}, {"total_tokens": 1}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run), \
            patch("hermes_cli.goals.GoalManager", stub):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/api/sessions/{session_id}/chat", json={"message": "hello"})
            assert resp.status == 200
            data = await resp.json()

    assert "goal" not in data
    assert calls["eval"] == []


@pytest.mark.asyncio
async def test_session_chat_returns_goal_decision_without_auto_continuing(adapter, session_db):
    """Non-streaming evaluates and reports; it must not run the continuation
    itself — that would hold one HTTP request open for another full turn."""
    session_id = session_db.create_session("goal-json", "api_server")
    stub, calls = _goal_manager_stub([
        {
            "status": "active",
            "should_continue": True,
            "continuation_prompt": "keep going",
            "verdict": "continue",
            "reason": "not done yet",
            "message": "↻ Continuing toward goal (1/20): not done yet",
        }
    ], turns_used=1, max_turns=20)
    runs = []

    async def fake_run(**kwargs):
        runs.append(kwargs["user_message"])
        return {"final_response": "partial answer", "session_id": session_id}, {"total_tokens": 3}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run), \
            patch("hermes_cli.goals.GoalManager", stub):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/api/sessions/{session_id}/chat", json={"message": "work on it"})
            assert resp.status == 200
            data = await resp.json()

    assert data["goal"] == {
        "status": "active",
        "verdict": "continue",
        "message": "↻ Continuing toward goal (1/20): not done yet",
        "should_continue": True,
        "continuation_prompt": "keep going",
        "turns_used": 1,
        "max_turns": 20,
    }
    # Keyed on the session id the goal meta row uses, and exactly one turn ran.
    assert calls["init"] == [session_id]
    assert runs == ["work on it"]
    # The judge sees the turn's visible answer.
    assert calls["eval"][0][0] == "partial answer"


@pytest.mark.asyncio
async def test_session_chat_survives_a_raising_goal_manager(adapter, session_db):
    """A broken goal judge must never cost the user their turn."""
    session_id = session_db.create_session("goal-boom", "api_server")

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("goal state is toast")

    async def fake_run(**kwargs):
        return {"final_response": "the answer", "session_id": session_id}, {"total_tokens": 2}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run), \
            patch("hermes_cli.goals.GoalManager", _Boom):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/api/sessions/{session_id}/chat", json={"message": "hello"})
            assert resp.status == 200
            data = await resp.json()

    assert data["message"]["content"] == "the answer"
    assert "goal" not in data


@pytest.mark.asyncio
async def test_session_chat_stream_runs_goal_continuation_as_a_distinct_turn(adapter, session_db):
    session_id = session_db.create_session("goal-stream", "api_server")
    stub, calls = _goal_manager_stub([
        {
            "status": "active",
            "should_continue": True,
            "continuation_prompt": "continue toward the goal",
            "verdict": "continue",
            "reason": "still work left",
            "message": "↻ Continuing toward goal (1/20): still work left",
        },
        {
            "status": "done",
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "done",
            "reason": "objective met",
            "message": "✓ Goal achieved: objective met",
        },
    ])
    runs = []

    async def fake_run(**kwargs):
        runs.append(kwargs["user_message"])
        kwargs["stream_delta_callback"](f"chunk-{len(runs)}")
        return {"final_response": f"answer-{len(runs)}", "session_id": session_id}, {"total_tokens": 5}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run), \
            patch("hermes_cli.goals.GoalManager", stub):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/api/sessions/{session_id}/chat/stream", json={"message": "start"})
            assert resp.status == 200
            body = await resp.text()

    events = _sse_events(body)
    names = [name for name, _ in events]
    # The continuation actually ran as a second agent turn.
    assert runs == ["start", "continue toward the goal"]
    assert names.count("goal.continuation") == 1
    assert names.count("message.started") == 2
    assert names.count("assistant.completed") == 2
    assert names.count("run.completed") == 1

    cont = next(payload for name, payload in events if name == "goal.continuation")
    assert cont["turn"] == 1
    assert cont["continuation_prompt"] == "continue toward the goal"
    assert cont["turns_used"] == 1 and cont["max_turns"] == 20

    started_ids = [p["message"]["id"] for n, p in events if n == "message.started"]
    assert started_ids[0] != started_ids[1]
    # The continuation announces the id its own frames will carry.
    assert cont["message_id"] == started_ids[1]
    deltas = [p for n, p in events if n == "assistant.delta"]
    assert deltas[0]["message_id"] == started_ids[0]
    assert deltas[1]["message_id"] == started_ids[1]

    # Both judge verdicts are surfaced, and the last one closes the goal out.
    status_msgs = [p["message"] for n, p in events if n == "goal.status"]
    assert status_msgs == [
        "↻ Continuing toward goal (1/20): still work left",
        "✓ Goal achieved: objective met",
    ]
    # One usage block for the request, summed across both turns.
    run_completed = next(p for n, p in events if n == "run.completed")
    assert run_completed["usage"]["total_tokens"] == 10
    assert run_completed["goal_continuations"] == 1


@pytest.mark.asyncio
async def test_session_chat_stream_goal_loop_is_bounded_by_the_per_request_cap(adapter, session_db):
    """A goal that never says "done" must not hold the connection forever."""
    from gateway.platforms.api_server import MAX_GOAL_CONTINUATIONS_PER_REQUEST

    session_id = session_db.create_session("goal-runaway", "api_server")
    stub, _calls = _goal_manager_stub([
        {
            "status": "active",
            "should_continue": True,
            "continuation_prompt": "again",
            "verdict": "continue",
            "reason": "forever",
            "message": "↻ Continuing",
        }
    ])
    runs = []

    async def fake_run(**kwargs):
        runs.append(kwargs["user_message"])
        return {"final_response": "still going", "session_id": session_id}, {"total_tokens": 1}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run), \
            patch("hermes_cli.goals.GoalManager", stub):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/api/sessions/{session_id}/chat/stream", json={"message": "go"})
            assert resp.status == 200
            body = await resp.text()

    events = _sse_events(body)
    names = [name for name, _ in events]
    assert len(runs) == 1 + MAX_GOAL_CONTINUATIONS_PER_REQUEST
    assert names.count("goal.continuation") == MAX_GOAL_CONTINUATIONS_PER_REQUEST
    assert names.count("run.completed") == 1
    capped = [p for n, p in events if n == "goal.status" and p.get("capped")]
    assert len(capped) == 1
    assert str(MAX_GOAL_CONTINUATIONS_PER_REQUEST) in capped[0]["message"]


@pytest.mark.asyncio
async def test_session_chat_stream_does_not_continue_a_stopped_run(adapter, session_db):
    """A stop accepted during the turn ends the request; the goal survives for
    the next one rather than being continued behind the user's back."""
    session_id = session_db.create_session("goal-stopped", "api_server")
    stub, calls = _goal_manager_stub([
        {
            "status": "active",
            "should_continue": True,
            "continuation_prompt": "again",
            "verdict": "continue",
            "reason": "more work",
            "message": "↻ Continuing",
        }
    ])
    runs = []

    async def fake_run(**kwargs):
        runs.append(kwargs["user_message"])
        # Stand in for POST /v1/runs/{run_id}/stop landing mid-turn.
        adapter._stopping_run_ids.update(adapter._run_statuses.keys())
        return {"final_response": "partial", "session_id": session_id}, {"total_tokens": 1}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run), \
            patch("hermes_cli.goals.GoalManager", stub):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/api/sessions/{session_id}/chat/stream", json={"message": "go"})
            assert resp.status == 200
            body = await resp.text()

    assert runs == ["go"]
    assert "event: goal.continuation" not in body
    # Not even the judge runs — a stopped run wants no more model calls.
    assert calls["eval"] == []


@pytest.mark.asyncio
async def test_session_chat_stream_survives_a_raising_goal_manager(adapter, session_db):
    session_id = session_db.create_session("goal-stream-boom", "api_server")

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("goal state is toast")

    async def fake_run(**kwargs):
        kwargs["stream_delta_callback"]("hello")
        return {"final_response": "hello", "session_id": session_id}, {"total_tokens": 1}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run), \
            patch("hermes_cli.goals.GoalManager", _Boom):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/api/sessions/{session_id}/chat/stream", json={"message": "go"})
            assert resp.status == 200
            body = await resp.text()

    assert "event: assistant.completed" in body
    assert "event: run.completed" in body
    assert "event: goal.continuation" not in body


# ---------------------------------------------------------------------------
# Per-request reasoning_effort (SwitchUI reasoning picker)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
async def test_session_chat_rejects_invalid_reasoning_effort(adapter, session_db, path):
    session_id = session_db.create_session("reasoning-bad", "api_server")
    mock_run = AsyncMock(return_value=({"final_response": "hi", "session_id": session_id}, {}))
    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", mock_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}{path}",
                json={"message": "hello", "reasoning_effort": "turbo"},
            )
            assert resp.status == 400
            payload = await resp.json()
    assert payload["error"]["code"] == "invalid_reasoning_effort"
    assert "turbo" in payload["error"]["message"]
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_session_chat_rejects_non_string_reasoning_effort(adapter, session_db):
    session_id = session_db.create_session("reasoning-nonstring", "api_server")
    mock_run = AsyncMock(return_value=({"final_response": "hi", "session_id": session_id}, {}))
    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", mock_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "hello", "reasoning_effort": 3},
            )
            assert resp.status == 400
            payload = await resp.json()
    assert payload["error"]["code"] == "invalid_reasoning_effort"
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_session_chat_forwards_reasoning_effort_to_the_turn(adapter, session_db):
    session_id = session_db.create_session("reasoning-forward", "api_server")
    mock_run = AsyncMock(return_value=({"final_response": "hi", "session_id": session_id}, {}))
    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", mock_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "hello", "reasoning_effort": "HIGH "},
            )
            assert resp.status == 200, await resp.text()
    captured = mock_run.call_args.kwargs
    assert captured["model_options"]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_session_chat_without_reasoning_effort_leaves_it_unset(adapter, session_db):
    session_id = session_db.create_session("reasoning-absent", "api_server")
    mock_run = AsyncMock(return_value=({"final_response": "hi", "session_id": session_id}, {}))
    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", mock_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/api/sessions/{session_id}/chat", json={"message": "hello"})
            assert resp.status == 200, await resp.text()
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("model_options", {}).get("reasoning_effort") is None


@pytest.mark.asyncio
async def test_session_chat_reasoning_effort_is_per_request_not_sticky(adapter, session_db):
    session_id = session_db.create_session("reasoning-sticky", "api_server")
    seen = []

    async def fake_run(**kwargs):
        seen.append(kwargs.get("model_options", {}).get("reasoning_effort"))
        return {"final_response": "hi", "session_id": session_id}, {}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "one", "reasoning_effort": "ultra"},
            )
            assert resp.status == 200, await resp.text()
            resp = await cli.post(f"/api/sessions/{session_id}/chat", json={"message": "two"})
            assert resp.status == 200, await resp.text()
    assert seen == ["ultra", None]


# ---------------------------------------------------------------------------
# usage.update SSE event (context_percent mirror for hermes-switchui context ring)
# ---------------------------------------------------------------------------


def _usage_update_events(body):
    """Pull every usage.update SSE frame's JSON payload out of an SSE body."""
    import json as _json

    events = []
    for block in body.split("\n\n"):
        if "event: usage.update" in block:
            for line in block.splitlines():
                if line.startswith("data: "):
                    events.append(_json.loads(line[len("data: "):]))
    return events


@pytest.mark.asyncio
async def test_session_chat_stream_emits_usage_update_on_call_and_compaction(adapter, session_db):
    """usage.update must fire after a model call and after a compaction, both
    mirroring context_usage_fields()'s context_percent (the same figure
    /api/sessions reports), never reimplementing the formula.
    """
    from agent.context_breakdown import context_usage_fields

    session_id = session_db.create_session("usage-update-session", "api_server")

    class _FakeCompressor:
        last_prompt_tokens = 4000
        context_length = 8000

    class _FakeAgent:
        context_compressor = _FakeCompressor()

    fake_agent = _FakeAgent()
    expected_percent = context_usage_fields(fake_agent.context_compressor)["context_percent"]

    async def fake_run(**kwargs):
        usage_callback = kwargs["usage_callback"]
        # (a) after a model call: not compacted, before == after.
        usage_callback(fake_agent, False, 3, 3)
        # (b) after a compaction: compacted, before != after.
        usage_callback(fake_agent, True, 10, 4)
        return {"final_response": "ok", "session_id": session_id}, {}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream",
                json={"message": "hi"},
            )
            assert resp.status == 200
            body = await resp.text()

    events = _usage_update_events(body)
    assert len(events) == 2, body

    call_event, compaction_event = events
    assert {k: call_event[k] for k in ("context_percent", "compacted", "messages_before", "messages_after")} == {
        "context_percent": expected_percent,
        "compacted": False,
        "messages_before": 3,
        "messages_after": 3,
    }
    assert {k: compaction_event[k] for k in ("context_percent", "compacted", "messages_before", "messages_after")} == {
        "context_percent": expected_percent,
        "compacted": True,
        "messages_before": 10,
        "messages_after": 4,
    }


@pytest.mark.asyncio
async def test_chat_completions_never_emits_usage_update(adapter):
    """usage.update is a session-chat-stream-only event; /v1/chat/completions
    must never wire a usage_callback nor emit the frame.
    """
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)

    async def fake_run(**kwargs):
        assert kwargs.get("usage_callback") is None
        return (
            {"final_response": "ok", "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    with patch.object(adapter, "_run_agent", side_effect=fake_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
            )
            assert resp.status == 200
            body = await resp.text()

    assert "usage.update" not in body
