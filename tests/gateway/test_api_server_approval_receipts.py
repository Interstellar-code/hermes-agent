import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

import hermes_state
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from tests.gateway.test_api_server_runs import _create_runs_app


@pytest.fixture
def adapter(monkeypatch, _isolate_hermes_home):
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
    ad = APIServerAdapter(PlatformConfig())
    return ad


@pytest.fixture
def session_db():
    db = SessionDB()
    try:
        yield db
    finally:
        db.close()


@pytest.mark.asyncio
async def test_run_approval_persists_receipt_and_emits_rich_payload(adapter, session_db):
    app = _create_runs_app(adapter)
    run_id = "run_receipt"
    session_id = session_db.create_session("approval-session", "api_server")
    adapter._run_statuses[run_id] = {"run_id": run_id, "status": "waiting_for_approval", "session_id": session_id}
    adapter._run_approval_sessions[run_id] = "session-123"
    adapter._run_approval_requests[run_id] = [{
        "approval_id": "approval_1",
        "session_id": session_id,
        "run_id": run_id,
        "message_id": "msg_1",
        "command": "rm -rf /tmp/demo",
        "description": "Delete temp dir",
        "choices": ["once", "session", "always", "deny"],
    }]
    q = MagicMock()
    adapter._run_streams[run_id] = q

    async with TestClient(TestServer(app)) as cli:
        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            resp = await cli.post(f"/v1/runs/{run_id}/approval", json={"choice": "once"})
            assert resp.status == 200, await resp.text()
            payload = await resp.json()

    mock_resolve.assert_called_once_with("session-123", "once", resolve_all=False)
    assert payload == {
        "object": "hermes.run.approval_response",
        "run_id": run_id,
        "approval_id": "approval_1",
        "choice": "once",
        "approved": True,
        "resolved": 1,
    }
    q.put_nowait.assert_called_once()
    event = q.put_nowait.call_args.args[0]
    assert event["event"] == "approval.responded"
    assert event["approval_id"] == "approval_1"
    assert event["session_id"] == session_id
    assert event["run_id"] == run_id
    assert event["message_id"] == "msg_1"
    assert event["choice"] == "once"
    assert event["approved"] is True
    assert event["resolved"] == 1
    assert event["action"] == "rm -rf /tmp/demo"
    assert event["context"] == "Delete temp dir"

    messages = session_db.get_messages(session_id)
    receipt = json.loads(messages[-1]["content"])
    assert receipt == {
        "type": "interaction_receipt",
        "kind": "approval",
        "tool_name": "approval",
        "approval_id": "approval_1",
        "session_id": session_id,
        "run_id": run_id,
        "message_id": "msg_1",
        "action": "rm -rf /tmp/demo",
        "context": "Delete temp dir",
        "pattern_key": None,
        "pattern_keys": None,
        "choices": ["once", "session", "always", "deny"],
        "selected_answer": "once",
        "approved": True,
        "resolved": 1,
    }


@pytest.mark.asyncio
async def test_run_approval_string_false_does_not_resolve_all_and_keeps_queue_order(adapter):
    app = _create_runs_app(adapter)
    run_id = "run_bool_parse"
    adapter._run_statuses[run_id] = {"run_id": run_id, "status": "running", "session_id": "sess-x"}
    adapter._run_approval_sessions[run_id] = "session-123"
    adapter._run_approval_requests[run_id] = [
        {"approval_id": "approval_oldest", "session_id": "sess-x", "run_id": run_id, "message_id": "m1", "choices": ["once", "session", "always", "deny"]},
        {"approval_id": "approval_newer", "session_id": "sess-x", "run_id": run_id, "message_id": "m2", "choices": ["once", "session", "always", "deny"]},
    ]

    async with TestClient(TestServer(app)) as cli:
        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve, \
             patch.object(adapter, "_persist_approval_receipt") as mock_receipt:
            approval_resp = await cli.post(
                f"/v1/runs/{run_id}/approval",
                json={"choice": "once", "all": "false"},
            )

    assert approval_resp.status == 200
    mock_resolve.assert_called_once_with("session-123", "once", resolve_all=False)
    mock_receipt.assert_called_once()
    approval_meta = mock_receipt.call_args.args[1]
    assert approval_meta["approval_id"] == "approval_oldest"
    assert adapter._run_approval_requests[run_id][0]["approval_id"] == "approval_newer"
