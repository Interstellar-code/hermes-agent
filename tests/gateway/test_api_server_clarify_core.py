import pytest
from unittest.mock import MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


@pytest.fixture
def adapter():
    ad = APIServerAdapter(PlatformConfig())
    ad._api_key = "test-key"
    return ad


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
    app.router.add_post("/api/sessions/{session_id}/chat/clarify", adapter._handle_session_clarify)
    return app


def test_default_config_disables_interactive_clarify():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["api_server"]["interactive_clarify"] is False


@patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
def test_create_agent_includes_clarify_only_with_both_gates(adapter):
    with (
        patch("gateway.run._resolve_runtime_agent_kwargs") as mock_kwargs,
        patch("gateway.run._resolve_gateway_model") as mock_model,
        patch("gateway.run._load_gateway_config") as mock_config,
        patch("run_agent.AIAgent") as mock_agent_cls,
    ):
        mock_kwargs.return_value = {"api_key": "test-key", "base_url": None, "provider": None, "api_mode": None, "command": None, "args": []}
        mock_model.return_value = "test/model"
        mock_agent_cls.return_value = MagicMock()

        mock_config.return_value = {"api_server": {"interactive_clarify": False}}
        adapter._create_agent(interactive_clarify=True)
        assert "clarify" not in mock_agent_cls.call_args.kwargs["enabled_toolsets"]

        mock_config.return_value = {"api_server": {"interactive_clarify": True}}
        adapter._create_agent(interactive_clarify=True)
        assert "clarify" in mock_agent_cls.call_args.kwargs["enabled_toolsets"]


@pytest.mark.asyncio
async def test_capabilities_advertises_clarify_surface(adapter):
    app = _app(adapter)
    with patch("gateway.run._load_gateway_config", return_value={"api_server": {"interactive_clarify": True}}):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/capabilities", headers={"Authorization": "Bearer test-key"})
            assert resp.status == 200
            data = await resp.json()
    assert data["features"]["interactive_clarify"] is True
    assert data["endpoints"]["session_chat_clarify"] == {
        "method": "POST",
        "path": "/api/sessions/{session_id}/chat/clarify",
    }


@pytest.mark.asyncio
async def test_session_clarify_endpoint_resolves_pending_clarify(adapter):
    from tools.clarify_gateway import clear_session, register

    session_id = "clarify-session"
    clarify_id = "clarify_1"
    events = []
    adapter._clarify_streams[session_id] = lambda name, payload: events.append((name, payload))
    register(clarify_id, session_id, "Pick one", ["A", "B"])
    adapter._session_interactions[clarify_id] = {
        "interaction_id": clarify_id,
        "clarify_id": clarify_id,
        "kind": "choice",
        "tool_name": "clarify",
        "session_id": session_id,
        "run_id": "run_1",
        "message_id": "msg_1",
        "question": "Pick one",
        "choices": ["A", "B"],
    }
    try:
        async with TestClient(TestServer(_app(adapter))) as cli:
            missing = await cli.post(f"/api/sessions/{session_id}/chat/clarify", json={"answer": "A"}, headers={"Authorization": "Bearer test-key"})
            assert missing.status == 400
            assert (await missing.json())["error"]["code"] == "clarify_id_required"

            ok = await cli.post(f"/api/sessions/{session_id}/chat/clarify", json={"clarify_id": clarify_id, "answer": "A"}, headers={"Authorization": "Bearer test-key"})
            assert ok.status == 200
            payload = await ok.json()
            assert payload["object"] == "hermes.session.clarify_response"
            assert payload["resolved"] is True

    finally:
        clear_session(session_id)

    event_names = [name for name, _ in events]
    assert event_names == ["clarify.responded", "interaction.responded"]
    responded = events[0][1]
    assert responded["clarify_id"] == clarify_id
    assert responded["interaction_id"] == clarify_id
    assert responded["question"] == "Pick one"
    assert responded["choices"] == ["A", "B"]
    assert responded["answer"] == "A"
    assert responded["selected_answer"] == "A"
    assert responded["resolved"] is True
