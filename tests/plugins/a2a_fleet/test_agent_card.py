"""US-002: Agent Card endpoint shape, security, public access."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="a2a_fleet server tests require hermes-agent[web]")
pytest.importorskip("uvicorn", reason="a2a_fleet server tests require hermes-agent[web]")

from fastapi.testclient import TestClient


def test_agent_card_public_and_well_formed(fleet_home) -> None:
    from a2a_fleet.server import build_app

    app = build_app()
    with TestClient(app) as client:
        response = client.get("/.well-known/agent-card.json")
    assert response.status_code == 200, "Agent Card must be reachable without auth"

    card = response.json()
    assert card["protocolVersion"] == "1.0"
    assert card["version"] == "0.1.0"
    assert card["name"] == "switch"
    assert card["url"] == "http://127.0.0.1:9319/jsonrpc"

    schemes = card["securitySchemes"]
    assert schemes["bearerAuth"]["type"] == "http"
    assert schemes["bearerAuth"]["scheme"] == "bearer"
    assert card["security"] == [{"bearerAuth": []}]
    assert card["capabilities"]["streaming"] is False, "v0.1 ships no streaming"

    skills = card["skills"]
    assert any(s["id"] == "echo" for s in skills)


def test_agent_card_ignores_bearer_header(fleet_home) -> None:
    """Agent Card must remain public even when a bearer header is supplied."""
    from a2a_fleet.server import build_app

    app = build_app()
    with TestClient(app) as client:
        response = client.get(
            "/.well-known/agent-card.json",
            headers={"authorization": "Bearer wrong-token"},
        )
    assert response.status_code == 200
