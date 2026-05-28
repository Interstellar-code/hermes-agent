"""Minimal A2A client for the a2a_fleet plugin.

Reads peer config from ``fleet.yaml`` and posts JSON-RPC ``SendMessage``
requests using ``httpx`` directly. No ``a2a-sdk`` dependency in v0.1.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from typing import Any, Dict, Optional

import httpx

from .fleet_config import get_agent


class FleetClientError(RuntimeError):
    """Raised when a peer request fails for any reason."""


def _peer_jsonrpc_url(agent_entry: Dict[str, Any]) -> str:
    base = (agent_entry.get("url") or "").rstrip("/")
    if not base:
        raise FleetClientError("agent entry missing 'url' in fleet.yaml")
    return f"{base}/jsonrpc"


def _send_message_payload(text: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"text": text}],
                "contextId": str(uuid.uuid4()),
            },
        },
    }


def _extract_reply(body: Dict[str, Any]) -> str:
    if "error" in body and body["error"]:
        err = body["error"]
        raise FleetClientError(
            f"peer returned JSON-RPC error {err.get('code')}: {err.get('message')}"
        )
    result = body.get("result") or {}
    if result.get("kind") != "message":
        raise FleetClientError(
            f"peer returned unexpected result kind {result.get('kind')!r}; "
            "v0.1 only supports kind='message'"
        )
    message = result.get("message") or {}
    parts = message.get("parts") or []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            return part["text"]
    raise FleetClientError("peer message has no text part")


async def send_message(
    agent_name: str,
    text: str,
    *,
    timeout: float = 30.0,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """Send ``text`` to the named peer and return the agent's reply text.

    Raises ``FleetClientError`` on auth failure, network failure, or unexpected
    response shape. The agent-side ``fleet_send`` tool wraps this in a
    ``{reply}|{error}`` dict so the agent never sees a raised exception.
    """
    try:
        entry = get_agent(agent_name)
    except KeyError as exc:
        raise FleetClientError(exc.args[0] if exc.args else "unknown agent") from exc
    url = _peer_jsonrpc_url(entry)
    payload = _send_message_payload(text)
    headers: Dict[str, str] = {"content-type": "application/json"}
    token = entry.get("token")
    if token:
        headers["authorization"] = f"Bearer {token}"

    own_client = client is None
    hc = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await hc.post(url, json=payload, headers=headers)
    except httpx.RequestError as exc:
        raise FleetClientError(f"network error talking to {agent_name!r} at {url}: {exc}") from exc
    finally:
        if own_client:
            await hc.aclose()

    if response.status_code == 401:
        raise FleetClientError(f"peer {agent_name!r} rejected bearer token (HTTP 401)")
    if response.status_code != 200:
        raise FleetClientError(
            f"peer {agent_name!r} returned HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise FleetClientError(f"peer {agent_name!r} returned non-JSON body") from exc
    return _extract_reply(body)


def _print_usage(exit_code: int = 0) -> None:
    print("usage: python -m a2a_fleet.client <agent_name> <message>", file=sys.stderr)
    print(
        "  HERMES_HOME must point at a profile dir whose fleet.yaml lists <agent_name>.",
        file=sys.stderr,
    )
    sys.exit(exit_code)


async def _amain(argv: list[str]) -> int:
    if len(argv) < 3:
        _print_usage(2)
    agent_name = argv[1]
    text = " ".join(argv[2:])
    try:
        reply = await send_message(agent_name, text)
    except FleetClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(reply)
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain(sys.argv)))


if __name__ == "__main__":
    main()
