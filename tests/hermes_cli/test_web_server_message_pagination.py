from starlette.testclient import TestClient

import hermes_state
from hermes_constants import get_hermes_home
from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
from hermes_state import SessionDB


def test_get_session_messages_honors_pagination_and_rejects_bad_limit(monkeypatch, _isolate_hermes_home):
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    db = SessionDB()
    try:
        db.create_session(session_id="paged-chat", source="cli")
        for i in range(6):
            db.append_message(session_id="paged-chat", role="user", content=f"m{i}")
    finally:
        db.close()

    resp = client.get("/api/sessions/paged-chat/messages?limit=2&offset=2")
    assert resp.status_code == 200
    body = resp.json()
    assert [m["content"] for m in body["messages"]] == ["m2", "m3"]

    # The dashboard endpoint is typed (limit/offset are Query(ge=0)), so bad
    # input is rejected by FastAPI's validator as 422 rather than the fork's
    # old hand-rolled 400. The gateway's aiohttp handler has no validator and
    # still returns 400 — see tests/gateway for that side.
    bad_text = client.get("/api/sessions/paged-chat/messages?limit=abc")
    assert bad_text.status_code == 422

    bad_negative = client.get("/api/sessions/paged-chat/messages?limit=-5")
    assert bad_negative.status_code == 422
