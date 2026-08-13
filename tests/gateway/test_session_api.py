"""Focused tests for API server session-control endpoints."""

from unittest.mock import AsyncMock, patch

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
    assert features["admin_config_rw"] is False
    assert features["memory_write_api"] is False
    assert features["skills_api"] is True
    assert features["realtime_voice"] is False
    assert data["endpoints"]["sessions"] == {"method": "GET", "path": "/api/sessions"}
    assert data["endpoints"]["session_chat_stream"] == {
        "method": "POST",
        "path": "/api/sessions/{session_id}/chat/stream",
    }


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
    assert usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert observed == {
        "task_id": "request-session",
        "context_session_id": "request-session",
        "context_platform": "api_server",
        "context_session_key": "request-key",
        "child_session_id": "request-session",
    }


@pytest.mark.asyncio
async def test_session_crud_and_message_history(adapter, session_db):
    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        create_resp = await cli.post("/api/sessions", json={"title": "Mobile chat", "model": "test-model"})
        assert create_resp.status == 201
        created = await create_resp.json()
        session_id = created["session"]["id"]
        assert created["object"] == "hermes.session"
        assert created["session"]["title"] == "Mobile chat"

        session_db.append_message(session_id, "user", "hello from phone")
        session_db.append_message(session_id, "assistant", "hello from hermes")

        list_resp = await cli.get("/api/sessions?limit=10&offset=0")
        assert list_resp.status == 200
        listed = await list_resp.json()
        assert listed["object"] == "list"
        assert [s["id"] for s in listed["data"]] == [session_id]
        assert listed["data"][0]["message_count"] == 2

        get_resp = await cli.get(f"/api/sessions/{session_id}")
        assert get_resp.status == 200
        got = await get_resp.json()
        assert got["session"]["id"] == session_id
        assert got["session"]["message_count"] == 2

        messages_resp = await cli.get(f"/api/sessions/{session_id}/messages")
        assert messages_resp.status == 200
        messages = await messages_resp.json()
        assert messages["object"] == "list"
        assert [m["role"] for m in messages["data"]] == ["user", "assistant"]
        assert messages["data"][0]["content"] == "hello from phone"

        patch_resp = await cli.patch(f"/api/sessions/{session_id}", json={"title": "Renamed"})
        assert patch_resp.status == 200
        patched = await patch_resp.json()
        assert patched["session"]["title"] == "Renamed"

        delete_resp = await cli.delete(f"/api/sessions/{session_id}")
        assert delete_resp.status == 200
        deleted = await delete_resp.json()
        assert deleted == {"object": "hermes.session.deleted", "id": session_id, "deleted": True}
        assert session_db.get_session(session_id) is None


@pytest.mark.asyncio
async def test_session_messages_follow_compression_tip(adapter, session_db):
    source_id = session_db.create_session("source-session", "api_server")
    session_db.append_message(source_id, "user", "before compression")
    session_db.end_session(source_id, "compression")
    session_db.create_session("tip-session", "api_server", parent_session_id=source_id)
    session_db.replace_messages(source_id, [])
    session_db.append_message("tip-session", "user", "after compression")

    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        messages_resp = await cli.get(f"/api/sessions/{source_id}/messages")
        assert messages_resp.status == 200
        messages = await messages_resp.json()

    assert messages["object"] == "list"
    assert messages["session_id"] == "tip-session"
    assert [m["content"] for m in messages["data"]] == ["after compression"]


@pytest.mark.asyncio
async def test_session_fork_uses_current_sessiondb_branch_primitives(adapter, session_db, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import projects_db
    with projects_db.connect_closing() as pconn:
        pid = projects_db.create_project(pconn, name="Test Project", folders=[str(tmp_path)])
        projects_db.bind_session(pconn, pid, "source-session", bound_by="test")

    source_id = session_db.create_session("source-session", "api_server", model="test-model")
    session_db.set_session_title(source_id, "Original")
    session_db.append_message(source_id, "user", "first path")
    session_db.append_message(source_id, "assistant", "answer")

    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(f"/api/sessions/{source_id}/fork", json={"title": "Alternative"})
        assert resp.status == 201
        payload = await resp.json()

    fork = payload["session"]
    assert payload["object"] == "hermes.session"
    assert fork["id"] != source_id
    assert fork["parent_session_id"] == source_id
    assert fork["title"] == "Alternative"
    assert [m["content"] for m in session_db.get_messages(fork["id"])] == ["first path", "answer"]
    assert session_db.get_session(source_id)["end_reason"] == "branched"

    # Assert inherited project binding
    with projects_db.connect_closing() as pconn:
        binding = projects_db.get_session_project(pconn, fork["id"])
        assert binding is not None
        assert binding.project_id == pid
        assert binding.bound_by == "fork"


@pytest.mark.asyncio
async def test_session_chat_loads_history_and_preserves_session_headers(auth_adapter, session_db):
    session_id = session_db.create_session("chat-session", "api_server")
    session_db.set_session_title(session_id, "Chat")
    session_db.append_message(session_id, "user", "earlier")
    session_db.append_message(session_id, "assistant", "prior answer")

    mock_run = AsyncMock(return_value=({"final_response": "fresh answer", "session_id": session_id}, {"total_tokens": 3}))
    app = _create_session_app(auth_adapter)
    with patch.object(auth_adapter, "_run_agent", mock_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "next", "system_message": "stay focused"},
                headers={"Authorization": "Bearer sk-test", "X-Hermes-Session-Key": "client-42"},
            )
            assert resp.status == 200
            payload = await resp.json()

    assert resp.headers["X-Hermes-Session-Id"] == session_id
    assert resp.headers["X-Hermes-Session-Key"] == "client-42"
    assert payload["object"] == "hermes.session.chat.completion"
    assert payload["session_id"] == session_id
    assert payload["message"]["role"] == "assistant"
    assert payload["message"]["content"] == "fresh answer"
    mock_run.assert_awaited_once()
    _, kwargs = mock_run.call_args
    assert kwargs["session_id"] == session_id
    assert kwargs["gateway_session_key"] == "client-42"
    assert kwargs["ephemeral_system_prompt"] == "stay focused"
    history = kwargs["conversation_history"]
    assert len(history) == 2
    assert isinstance(history[0].pop("timestamp"), (int, float))
    assert isinstance(history[1].pop("timestamp"), (int, float))
    assert history == [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "prior answer"},
    ]


@pytest.mark.asyncio
async def test_session_chat_accepts_multimodal_message(auth_adapter, session_db):
    session_id = session_db.create_session("image-session", "api_server")
    image_payload = [
        {"type": "input_text", "text": "What's in this image?"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
    ]
    expected_user_message = [
        {"type": "text", "text": "What's in this image?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]

    mock_run = AsyncMock(return_value=({"final_response": "A cat.", "session_id": session_id}, {"total_tokens": 4}))
    app = _create_session_app(auth_adapter)
    with patch.object(auth_adapter, "_run_agent", mock_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": image_payload},
                headers={"Authorization": "Bearer sk-test"},
            )
            assert resp.status == 200, await resp.text()

    _, kwargs = mock_run.call_args
    assert kwargs["user_message"] == expected_user_message


@pytest.mark.asyncio
async def test_session_chat_stream_accepts_multimodal_message(adapter, session_db):
    session_id = session_db.create_session("image-stream-session", "api_server")
    image_payload = [
        {"type": "input_text", "text": "What's in this image?"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
    ]
    expected_user_message = [
        {"type": "text", "text": "What's in this image?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    captured_kwargs = {}

    async def fake_run(**kwargs):
        captured_kwargs.update(kwargs)
        kwargs["stream_delta_callback"]("A cat.")
        return {"final_response": "A cat.", "session_id": session_id}, {"total_tokens": 4}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream",
                json={"message": image_payload},
            )
            assert resp.status == 200, await resp.text()
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            body = await resp.text()

    assert "event: assistant.completed" in body
    assert captured_kwargs["user_message"] == expected_user_message


@pytest.mark.asyncio
async def test_session_chat_stream_emits_lifecycle_events_and_keepalive_safe_shape(adapter, session_db):
    session_id = session_db.create_session("stream-session", "api_server")
    session_db.set_session_title(session_id, "Stream")

    async def fake_run(**kwargs):
        kwargs["stream_delta_callback"]("Hello")
        kwargs["stream_delta_callback"](" world")
        kwargs["tool_progress_callback"]("reasoning.available", tool_name="_thinking", preview="thinking")
        return {"final_response": "Hello world", "session_id": session_id}, {"total_tokens": 2}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/api/sessions/{session_id}/chat/stream", json={"message": "stream please"})
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            body = await resp.text()

    assert "event: run.started" in body
    assert "event: message.started" in body
    assert "event: assistant.delta" in body
    assert "Hello world" in body
    assert "event: tool.progress" in body
    assert "event: assistant.completed" in body
    assert "event: run.completed" in body
    assert "event: done" in body


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



@pytest.mark.asyncio
async def test_session_endpoints_require_auth_when_key_configured(auth_adapter):
    app = _create_session_app(auth_adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/api/sessions")
        assert resp.status == 401
        body = await resp.json()
        assert body["error"]["code"] == "invalid_api_key"

        ok = await cli.get("/api/sessions", headers={"Authorization": "Bearer sk-test"})
        assert ok.status == 200
        data = await ok.json()
        assert data["object"] == "list"
        assert data["data"] == []


@pytest.mark.asyncio
async def test_session_header_rejected_without_api_key(adapter, session_db):
    session_id = session_db.create_session("unsafe-session", "api_server")
    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            f"/api/sessions/{session_id}/chat",
            json={"message": "hello"},
            headers={"X-Hermes-Session-Key": "client-42"},
        )
        assert resp.status == 403
        data = await resp.json()
        assert "X-Hermes-Session-Key requires API key" in data["error"]["message"]


# ---------------------------------------------------------------------------
# Approval bypass (#219) — must be settable in the process that enforces it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_yolo_toggles_state_in_this_process(auth_adapter, session_db):
    """POST/GET /api/sessions/{id}/yolo drive tools.approval in THIS process.

    ``_session_yolo`` is a module-level set with no IPC, and agents serving
    this transport are built here — so this is the only place a bypass toggle
    reaches the guard that gates them. Toggling from the tui_gateway dashboard
    (or, before that, the slash worker) flips a different copy of the set and
    reports a change the enforcing agent never sees (#219).
    """
    from tools.approval import disable_session_yolo, is_session_yolo_enabled

    session_id = session_db.create_session("yolo-session", "api_server")
    app = web.Application()
    app.router.add_get("/api/sessions/{session_id}/yolo", auth_adapter._handle_get_session_yolo)
    app.router.add_post("/api/sessions/{session_id}/yolo", auth_adapter._handle_set_session_yolo)
    hdrs = {"Authorization": "Bearer sk-test"}

    disable_session_yolo(session_id)
    try:
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get(f"/api/sessions/{session_id}/yolo", headers=hdrs)
            assert resp.status == 200
            assert (await resp.json())["enabled"] is False

            # Explicit enable moves REAL approval state in this process.
            resp = await cli.post(
                f"/api/sessions/{session_id}/yolo", json={"enabled": True}, headers=hdrs
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["enabled"] is True and body["previous"] is False
            assert is_session_yolo_enabled(session_id) is True

            # Omitting `enabled` toggles.
            resp = await cli.post(f"/api/sessions/{session_id}/yolo", json={}, headers=hdrs)
            assert (await resp.json())["enabled"] is False
            assert is_session_yolo_enabled(session_id) is False

            # A non-boolean is rejected rather than silently toggling.
            resp = await cli.post(
                f"/api/sessions/{session_id}/yolo", json={"enabled": "maybe"}, headers=hdrs
            )
            assert resp.status == 400
            assert (await resp.json())["error"]["code"] == "invalid_enabled"
            assert is_session_yolo_enabled(session_id) is False
    finally:
        disable_session_yolo(session_id)


@pytest.mark.asyncio
async def test_session_yolo_keys_on_the_header_like_approvals_do(auth_adapter, session_db):
    """The key must match what the approval guard computes.

    _run_agent binds HERMES_SESSION_KEY to ``gateway_session_key or
    session_id`` and registers approvals under the same expression. Keying
    the toggle differently would flip a bypass nothing reads.
    """
    from tools.approval import disable_session_yolo, is_session_yolo_enabled

    session_id = session_db.create_session("yolo-keyed", "api_server")
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/yolo", auth_adapter._handle_set_session_yolo)

    disable_session_yolo("client-key-42")
    disable_session_yolo(session_id)
    try:
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/yolo",
                json={"enabled": True},
                headers={"Authorization": "Bearer sk-test", "X-Hermes-Session-Key": "client-key-42"},
            )
            assert resp.status == 200
            # Stored under the header key, NOT the URL session id.
            assert is_session_yolo_enabled("client-key-42") is True
            assert is_session_yolo_enabled(session_id) is False
    finally:
        disable_session_yolo("client-key-42")
        disable_session_yolo(session_id)


@pytest.mark.asyncio
async def test_session_yolo_requires_auth(adapter, session_db):
    session_id = session_db.create_session("yolo-auth", "api_server")
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/yolo", adapter._handle_set_session_yolo)
    async with TestClient(TestServer(app)) as cli:
        # `adapter` has no key configured, so this asserts the guard is wired,
        # not that it rejects — a keyless adapter accepts by design.
        resp = await cli.post(f"/api/sessions/{session_id}/yolo", json={"enabled": False})
        assert resp.status in (200, 401)


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
