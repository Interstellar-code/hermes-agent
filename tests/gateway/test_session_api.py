"""Focused tests for API server session-control endpoints."""

import json
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
    app.router.add_post("/api/sessions/{session_id}/chat/clarify", adapter._handle_session_clarify)
    app.router.add_post(
        "/api/sessions/{session_id}/chat/interactions/{interaction_id}/respond",
        adapter._handle_session_interaction_respond,
    )
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
    assert data["endpoints"]["session_chat_clarify"] == {
        "method": "POST",
        "path": "/api/sessions/{session_id}/chat/clarify",
    }
    assert data["endpoints"]["session_chat_interaction_respond"] == {
        "method": "POST",
        "path": "/api/sessions/{session_id}/chat/interactions/{interaction_id}/respond",
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
async def test_session_fork_uses_current_sessiondb_branch_primitives(adapter, session_db):
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


@pytest.mark.asyncio
async def test_session_clarify_endpoint_statuses_events_and_persists_receipt(adapter, session_db):
    from tools.clarify_gateway import clear_session, register

    session_id = session_db.create_session("clarify-session", "api_server")
    app = _create_session_app(adapter)
    events = []
    adapter._clarify_streams[session_id] = lambda name, payload: events.append((name, payload))
    clarify_id = "clarify_1"
    register(clarify_id, session_id, "Pick a backend path?", ["Core", "Plugin"])
    adapter._session_interactions[clarify_id] = {
        "interaction_id": clarify_id,
        "clarify_id": clarify_id,
        "kind": "choice",
        "tool_name": "clarify",
        "session_id": session_id,
        "run_id": "run_1",
        "message_id": "msg_1",
        "question": "Pick a backend path?",
        "choices": ["Core", "Plugin"],
    }

    try:
        async with TestClient(TestServer(app)) as cli:
            missing = await cli.post(f"/api/sessions/{session_id}/chat/clarify", json={"answer": "Core"})
            assert missing.status == 400
            assert (await missing.json())["error"]["code"] == "clarify_id_required"

            ok = await cli.post(
                f"/api/sessions/{session_id}/chat/clarify",
                json={"clarify_id": clarify_id, "answer": "Core"},
            )
            assert ok.status == 200, await ok.text()
            payload = await ok.json()
            assert payload["object"] == "hermes.session.clarify_response"
            assert payload["clarify_id"] == clarify_id
            assert payload["resolved"] is True

            stale = await cli.post(
                f"/api/sessions/{session_id}/chat/clarify",
                json={"clarify_id": clarify_id, "answer": "Plugin"},
            )
            assert stale.status == 409
            assert (await stale.json())["error"]["code"] == "clarify_not_pending"
    finally:
        clear_session(session_id)

    event_names = [name for name, _ in events]
    assert event_names == ["clarify.responded", "interaction.responded"]
    responded = events[0][1]
    assert responded["clarify_id"] == clarify_id
    assert responded["interaction_id"] == clarify_id
    assert responded["run_id"] == "run_1"
    assert responded["message_id"] == "msg_1"
    assert responded["question"] == "Pick a backend path?"
    assert responded["choices"] == ["Core", "Plugin"]
    assert responded["answer"] == "Core"
    assert responded["selected_answer"] == "Core"
    assert responded["resolved"] is True

    messages = session_db.get_messages(session_id)
    receipt_msg = messages[-1]
    assert receipt_msg["role"] == "tool"
    assert receipt_msg["tool_name"] == "clarify"
    receipt = json.loads(receipt_msg["content"])
    assert receipt == {
        "type": "interaction_receipt",
        "kind": "choice",
        "tool_name": "clarify",
        "interaction_id": clarify_id,
        "clarify_id": clarify_id,
        "session_id": session_id,
        "run_id": "run_1",
        "message_id": "msg_1",
        "question": "Pick a backend path?",
        "choices": ["Core", "Plugin"],
        "selected_answer": "Core",
        "resolved": True,
    }


@pytest.mark.asyncio
async def test_session_interaction_respond_endpoint_resolves_clarify(adapter, session_db):
    from tools.clarify_gateway import clear_session, register

    session_id = session_db.create_session("interaction-session", "api_server")
    interaction_id = "interaction_1"
    register(interaction_id, session_id, "Type a value", None)
    adapter._session_interactions[interaction_id] = {
        "interaction_id": interaction_id,
        "clarify_id": interaction_id,
        "kind": "text",
        "tool_name": "clarify",
        "session_id": session_id,
        "run_id": "run_2",
        "message_id": "msg_2",
        "question": "Type a value",
        "choices": None,
    }

    app = _create_session_app(adapter)
    try:
        async with TestClient(TestServer(app)) as cli:
            ok = await cli.post(
                f"/api/sessions/{session_id}/chat/interactions/{interaction_id}/respond",
                json={"answer": "free text"},
            )
            assert ok.status == 200, await ok.text()
            payload = await ok.json()
            assert payload["object"] == "hermes.session.interaction_response"
            assert payload["interaction_id"] == interaction_id
            assert payload["clarify_id"] == interaction_id
            assert payload["resolved"] is True

            stale = await cli.post(
                f"/api/sessions/{session_id}/chat/interactions/{interaction_id}/respond",
                json={"answer": "again"},
            )
            assert stale.status == 409
            assert (await stale.json())["error"]["code"] == "interaction_not_pending"
    finally:
        clear_session(session_id)

    receipt = json.loads(session_db.get_messages(session_id)[-1]["content"])
    assert receipt["kind"] == "text"
    assert receipt["question"] == "Type a value"
    assert receipt["choices"] is None
    assert receipt["selected_answer"] == "free text"
    assert receipt["resolved"] is True


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
    assert kwargs["conversation_history"] == [
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
async def test_run_approval_response_emits_event_and_persists_receipt(adapter, session_db):
    session_id = session_db.create_session("approval-session", "api_server")
    run_id = "run_approval_test"
    approval_id = "approval_1"
    approval_session_key = "approval-session-key"
    adapter._run_statuses[run_id] = {"status": "waiting_for_approval", "session_id": session_id}
    adapter._run_approval_sessions[run_id] = approval_session_key
    queue = __import__("asyncio").Queue()
    adapter._run_streams[run_id] = queue
    adapter._run_approval_requests[run_id] = [{
        "event": "approval.request",
        "approval_id": approval_id,
        "session_id": session_id,
        "run_id": run_id,
        "message_id": "msg_approval",
        "timestamp": 123.0,
        "command": "rm -rf /tmp/example",
        "description": "Dangerous command",
        "pattern_key": "dangerous_rm",
        "pattern_keys": ["dangerous_rm"],
        "choices": ["once", "session", "always", "deny"],
    }]

    app = _create_session_app(adapter)
    app.router.add_post("/v1/runs/{run_id}/approval", adapter._handle_run_approval)
    with patch("tools.approval.resolve_gateway_approval", return_value=1):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/v1/runs/{run_id}/approval", json={"choice": "approve"})
            assert resp.status == 200, await resp.text()
            payload = await resp.json()

    assert payload["object"] == "hermes.run.approval_response"
    assert payload["approval_id"] == approval_id
    assert payload["choice"] == "once"
    assert payload["approved"] is True
    assert payload["resolved"] == 1

    event = queue.get_nowait()
    assert event["event"] == "approval.responded"
    assert event["approval_id"] == approval_id
    assert event["session_id"] == session_id
    assert event["run_id"] == run_id
    assert event["message_id"] == "msg_approval"
    assert event["choice"] == "once"
    assert event["approved"] is True
    assert event["resolved"] == 1
    assert event["action"] == "rm -rf /tmp/example"
    assert event["context"] == "Dangerous command"
    assert event["choices"] == ["once", "session", "always", "deny"]

    receipt_msg = session_db.get_messages(session_id)[-1]
    assert receipt_msg["role"] == "tool"
    assert receipt_msg["tool_name"] == "approval"
    receipt = json.loads(receipt_msg["content"])
    assert receipt["kind"] == "approval"
    assert receipt["tool_name"] == "approval"
    assert receipt["approval_id"] == approval_id
    assert receipt["action"] == "rm -rf /tmp/example"
    assert receipt["context"] == "Dangerous command"
    assert receipt["selected_answer"] == "once"
    assert receipt["approved"] is True
    assert receipt["resolved"] == 1


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
