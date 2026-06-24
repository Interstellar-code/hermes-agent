from starlette.testclient import TestClient

import hermes_state
from hermes_constants import get_hermes_home
from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
from hermes_state import SessionDB


def test_dashboard_session_endpoints_still_work_with_threaded_db(monkeypatch, _isolate_hermes_home):
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    db = SessionDB()
    try:
        db.create_session(session_id="threaded-chat", source="cli")
        db.append_message(session_id="threaded-chat", role="user", content="hello")
        db.create_session(session_id="empty-chat", source="cli")
        db.end_session("empty-chat", "done")
    finally:
        db.close()

    status = client.get("/api/status")
    assert status.status_code == 200
    assert "active_sessions" in status.json()

    sessions = client.get("/api/sessions")
    assert sessions.status_code == 200
    assert any(s.get("id") == "threaded-chat" for s in sessions.json()["sessions"])

    stats = client.get("/api/sessions/stats")
    assert stats.status_code == 200
    assert stats.json()["messages"] >= 1

    empty_count = client.get("/api/sessions/empty/count")
    assert empty_count.status_code == 200
    assert empty_count.json()["count"] >= 1

    deleted = client.delete("/api/sessions/empty")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True
