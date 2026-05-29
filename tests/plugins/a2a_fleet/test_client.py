"""US-004: client.send_message — envelope shape, bearer header, error paths."""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest


def test_send_message_envelope_and_bearer(fleet_home: Path) -> None:
    from a2a_fleet.client import send_message

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        import json

        body = json.loads(request.content.decode())
        captured["method"] = body["method"]
        text = body["params"]["message"]["parts"][0]["text"]
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "kind": "message",
                    "message": {"role": "agent", "parts": [{"text": f"echo:{text}"}]},
                },
            },
        )

    transport = httpx.MockTransport(handler)

    async def go():
        async with httpx.AsyncClient(transport=transport) as hc:
            return await send_message("construct", "hi there", client=hc)

    reply = asyncio.run(go())
    assert reply == "echo:hi there"
    assert captured["method"] == "SendMessage"
    assert captured["auth"] == "Bearer tok-construct"
    assert captured["url"].endswith("/jsonrpc")


def test_send_message_unknown_agent_raises(fleet_home: Path) -> None:
    from a2a_fleet.client import FleetClientError, send_message

    async def go():
        return await send_message("does-not-exist", "ping")

    with pytest.raises(FleetClientError):
        asyncio.run(go())


def test_send_message_propagates_peer_error(fleet_home: Path) -> None:
    from a2a_fleet.client import FleetClientError, send_message

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "x",
                "error": {"code": -32601, "message": "Method not found: SendMessage"},
            },
        )

    transport = httpx.MockTransport(handler)

    async def go():
        async with httpx.AsyncClient(transport=transport) as hc:
            return await send_message("construct", "ping", client=hc)

    with pytest.raises(FleetClientError) as exc:
        asyncio.run(go())
    assert "-32601" in str(exc.value)


def test_send_message_http_401_raises(fleet_home: Path) -> None:
    from a2a_fleet.client import FleetClientError, send_message

    transport = httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": "missing bearer token"})
    )

    async def go():
        async with httpx.AsyncClient(transport=transport) as hc:
            return await send_message("construct", "ping", client=hc)

    with pytest.raises(FleetClientError) as exc:
        asyncio.run(go())
    assert "401" in str(exc.value)
