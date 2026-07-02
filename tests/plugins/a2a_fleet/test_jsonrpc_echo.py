"""US-003: JSON-RPC SendMessage echo + bearer auth + error codes."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="a2a_fleet server tests require hermes-agent[web]")
pytest.importorskip("uvicorn", reason="a2a_fleet server tests require hermes-agent[web]")

from pathlib import Path

import yaml
from fastapi.testclient import TestClient


def _send_message_body(text: str = "ping"):
    return {
        "jsonrpc": "2.0",
        "id": "test-1",
        "method": "SendMessage",
        "params": {"message": {"role": "user", "parts": [{"text": text}]}},
    }


def test_ping_returns_pong_message(fleet_home: Path) -> None:
    from a2a_fleet.server import build_app

    with TestClient(build_app()) as client:
        response = client.post("/jsonrpc", json=_send_message_body("ping"))
    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["result"]["kind"] == "message"
    message = body["result"]["message"]
    assert message["role"] == "agent"
    assert message["parts"][0]["text"] == "pong"


def test_arbitrary_text_is_echoed(fleet_home: Path) -> None:
    from a2a_fleet.server import build_app

    with TestClient(build_app()) as client:
        response = client.post("/jsonrpc", json=_send_message_body("hello world"))
    assert response.json()["result"]["message"]["parts"][0]["text"] == "hello world"


def test_malformed_body_returns_parse_error(fleet_home: Path) -> None:
    from a2a_fleet.server import build_app

    with TestClient(build_app()) as client:
        response = client.post(
            "/jsonrpc",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32700


def test_unknown_method_returns_method_not_found(fleet_home: Path) -> None:
    from a2a_fleet.server import build_app

    body = {"jsonrpc": "2.0", "id": "x", "method": "tasks.get", "params": {}}
    with TestClient(build_app()) as client:
        response = client.post("/jsonrpc", json=body)
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32601


def test_message_send_alias_returns_same_envelope(fleet_home: Path) -> None:
    from a2a_fleet.server import build_app

    body = {
        "jsonrpc": "2.0",
        "id": "alias-1",
        "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"text": "ping"}]}},
    }
    with TestClient(build_app()) as client:
        response = client.post("/jsonrpc", json=body)
    assert response.status_code == 200
    result = response.json()
    assert result["jsonrpc"] == "2.0"
    assert result["result"]["kind"] == "message"
    assert result["result"]["message"]["parts"][0]["text"] == "pong"


def test_message_stream_returns_not_implemented(fleet_home: Path) -> None:
    from a2a_fleet.server import build_app

    body = {"jsonrpc": "2.0", "id": "stream-1", "method": "message/stream", "params": {}}
    with TestClient(build_app()) as client:
        response = client.post("/jsonrpc", json=body)
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32601


def test_bearer_enforced_when_auth_required(fleet_home: Path) -> None:
    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_yaml.read_text())
    data["fleet"]["server"]["auth_required"] = True
    fleet_yaml.write_text(yaml.safe_dump(data))

    from a2a_fleet.server import build_app

    with TestClient(build_app()) as client:
        # No bearer
        r1 = client.post("/jsonrpc", json=_send_message_body("ping"))
        assert r1.status_code == 401

        # Wrong bearer
        r2 = client.post(
            "/jsonrpc",
            json=_send_message_body("ping"),
            headers={"authorization": "Bearer wrong"},
        )
        assert r2.status_code == 401

        # Correct bearer
        r3 = client.post(
            "/jsonrpc",
            json=_send_message_body("ping"),
            headers={"authorization": "Bearer tok-switch"},
        )
        assert r3.status_code == 200
        assert r3.json()["result"]["message"]["parts"][0]["text"] == "pong"

        # Agent Card stays public
        r4 = client.get("/.well-known/agent-card.json")
        assert r4.status_code == 200
