"""Regression tests for profile-scoped dashboard session endpoints."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")


def _seed_session(db_path: Path, session_id: str, content: str) -> None:
    from hermes_state import SessionDB

    db = SessionDB(db_path=db_path)
    try:
        db.create_session(session_id, "cli", model="test-model")
        db.append_message(session_id, "user", content)
    finally:
        db.close()


def test_sessions_endpoint_uses_requested_profile_db(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    home = tmp_path / ".hermes"
    default_db = home / "state.db"
    profile_dir = home / "profiles" / "alpha"
    profile_db = profile_dir / "state.db"
    profile_dir.mkdir(parents=True)
    home.mkdir(exist_ok=True)

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    _seed_session(default_db, "default-session", "default needle")
    _seed_session(profile_db, "alpha-session", "alpha needle")

    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    default_resp = client.get("/api/sessions", params={"profile": "default"})
    assert default_resp.status_code == 200
    default_data = default_resp.json()
    assert default_data["profile"] == "default"
    assert [s["id"] for s in default_data["sessions"]] == ["default-session"]
    assert default_data["sessions"][0]["profile"] == "default"

    profile_resp = client.get("/api/sessions", params={"profile": "alpha"})
    assert profile_resp.status_code == 200
    profile_data = profile_resp.json()
    assert profile_data["profile"] == "alpha"
    assert [s["id"] for s in profile_data["sessions"]] == ["alpha-session"]
    assert profile_data["sessions"][0]["profile"] == "alpha"


def test_session_search_endpoint_uses_requested_profile_db(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    home = tmp_path / ".hermes"
    default_db = home / "state.db"
    profile_dir = home / "profiles" / "alpha"
    profile_db = profile_dir / "state.db"
    profile_dir.mkdir(parents=True)
    home.mkdir(exist_ok=True)

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    _seed_session(default_db, "default-session", "sharedterm default-only")
    _seed_session(profile_db, "alpha-session", "sharedterm alpha-only")

    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    default_resp = client.get("/api/sessions/search", params={"q": "sharedterm", "profile": "default"})
    assert default_resp.status_code == 200
    default_data = default_resp.json()
    assert default_data["profile"] == "default"
    assert {r["session_id"] for r in default_data["results"]} == {"default-session"}
    assert {r["profile"] for r in default_data["results"]} == {"default"}

    profile_resp = client.get(
        "/api/sessions/search", params={"q": "sharedterm", "profile": "alpha"}
    )
    assert profile_resp.status_code == 200
    profile_data = profile_resp.json()
    assert profile_data["profile"] == "alpha"
    assert {r["session_id"] for r in profile_data["results"]} == {"alpha-session"}
    assert {r["profile"] for r in profile_data["results"]} == {"alpha"}
