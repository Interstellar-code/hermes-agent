import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


@pytest.fixture
def adapter():
    ad = APIServerAdapter(PlatformConfig())
    ad._api_key = "test-key"
    return ad


@pytest.fixture
def session_db(monkeypatch, _isolate_hermes_home):
    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
    db = SessionDB()
    try:
        yield db
    finally:
        db.close()


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat/clarify", adapter._handle_session_clarify)
    app.router.add_post(
        "/api/sessions/{session_id}/chat/interactions/{interaction_id}/respond",
        adapter._handle_session_interaction_respond,
    )
    app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
    return app


@pytest.mark.asyncio
async def test_capabilities_advertises_interaction_respond(adapter):
    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await cli.get("/v1/capabilities", headers={"Authorization": "Bearer test-key"})
        assert resp.status == 200
        data = await resp.json()
    assert data["endpoints"]["session_chat_interaction_respond"] == {
        "method": "POST",
        "path": "/api/sessions/{session_id}/chat/interactions/{interaction_id}/respond",
    }


@pytest.mark.asyncio
async def test_session_clarify_endpoint_statuses_events_and_persists_receipt(adapter, session_db):
    from tools.clarify_gateway import clear_session, register

    session_id = session_db.create_session("clarify-session", "api_server")
    app = _app(adapter)
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
            missing = await cli.post(
                f"/api/sessions/{session_id}/chat/clarify",
                json={"answer": "Core"},
                headers={"Authorization": "Bearer test-key"},
            )
            assert missing.status == 400
            assert (await missing.json())["error"]["code"] == "clarify_id_required"

            ok = await cli.post(
                f"/api/sessions/{session_id}/chat/clarify",
                json={"clarify_id": clarify_id, "answer": "Core"},
                headers={"Authorization": "Bearer test-key"},
            )
            assert ok.status == 200, await ok.text()
            payload = await ok.json()
            assert payload["object"] == "hermes.session.clarify_response"
            assert payload["clarify_id"] == clarify_id
            assert payload["resolved"] is True
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

    try:
        async with TestClient(TestServer(_app(adapter))) as cli:
            ok = await cli.post(
                f"/api/sessions/{session_id}/chat/interactions/{interaction_id}/respond",
                json={"answer": "free text"},
                headers={"Authorization": "Bearer test-key"},
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
                headers={"Authorization": "Bearer test-key"},
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
