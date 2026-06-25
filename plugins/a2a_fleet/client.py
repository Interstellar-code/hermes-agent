"""Minimal A2A client for the a2a_fleet plugin.

Reads peer config from ``fleet.yaml`` and posts JSON-RPC ``SendMessage``
requests using ``httpx`` directly. No ``a2a-sdk`` dependency in v0.1.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
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


def _send_message_payload(text: str, context_id: Optional[str] = None) -> Dict[str, Any]:
    message: Dict[str, Any] = {
        "role": "user",
        "parts": [{"text": text}],
    }
    if context_id is not None:
        message["contextId"] = context_id
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "SendMessage",
        "params": {"message": message},
    }


def _extract_reply(body: Dict[str, Any]) -> Dict[str, str]:
    """Return ``{"reply": ..., "context_id": ...}`` from a JSON-RPC response body."""
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
            return {
                "reply": part["text"],
                "context_id": message.get("contextId", ""),
            }
    raise FleetClientError("peer message has no text part")


async def send_message(
    agent_name: str,
    text: str,
    *,
    context_id: Optional[str] = None,
    timeout: float = 30.0,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, str]:
    """Send ``text`` to the named peer and return ``{"reply": ..., "context_id": ...}``.

    When ``context_id`` is given it is included in ``params.message.contextId``.
    When omitted the server generates one and returns it in the response.

    Raises ``FleetClientError`` on auth failure, network failure, or unexpected
    response shape. The agent-side ``fleet_send`` tool wraps this in a
    ``{reply, context_id}|{error}`` dict so the agent never sees a raised exception.
    """
    try:
        entry = get_agent(agent_name)
    except KeyError as exc:
        raise FleetClientError(exc.args[0] if exc.args else "unknown agent") from exc
    url = _peer_jsonrpc_url(entry)
    payload = _send_message_payload(text, context_id=context_id)
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


# ---------------------------------------------------------------------------
# Wait-for-reply helper (P0-4)
#
# Some receivers (notably the opencode receiver) cannot finish inside the HTTP
# request: they return an immediate "[queued]" ack and write the real reply to
# a local transcript jsonl later, also POSTing it back to Hermes. For the
# CLI/testing path there is no inbound listener, so callers had to sleep + tail
# the transcript by hand. ``send_message_and_wait`` automates exactly that poll.
# ---------------------------------------------------------------------------

QUEUED_MARKER = "[queued]"
OC_TRANSCRIPT_REL = ".hermes/a2a-oc-transcript.jsonl"


def _resolve_transcript_path(
    agent_entry: Dict[str, Any], transcript_path: Optional[str]
) -> Optional[Path]:
    if transcript_path:
        return Path(transcript_path)
    repo = agent_entry.get("repo_path")
    if repo:
        return Path(repo) / OC_TRANSCRIPT_REL
    return None


def _scan_transcript_reply(
    path: Path, context_id: str, start_offset: int
) -> tuple[Optional[str], int]:
    """Scan transcript bytes appended after ``start_offset`` for the peer's final
    reply on ``context_id``. Returns ``(reply_text_or_None, new_offset)``.

    A "final" reply is a record addressed ``to == "hermes"`` from the peer that
    is not the ``[queued]`` ack and not a ``(busy)`` notice.
    """
    try:
        if path.stat().st_size <= start_offset:
            return None, start_offset
    except OSError:
        return None, start_offset
    with path.open("r") as f:
        f.seek(start_offset)
        data = f.read()
        new_offset = f.tell()
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("contextId") != context_id:
            continue
        if rec.get("to") != "hermes" or rec.get("from") == "hermes":
            continue
        direction = rec.get("dir", "")
        if "(ack)" in direction or "(busy)" in direction:
            continue
        text = rec.get("text", "")
        if QUEUED_MARKER in text:
            continue
        return text, new_offset
    return None, new_offset


async def send_message_and_wait(
    agent_name: str,
    text: str,
    *,
    context_id: Optional[str] = None,
    transcript_path: Optional[str] = None,
    max_wait: float = 280.0,
    poll_interval: float = 2.0,
    send_timeout: float = 30.0,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Send ``text`` and block until the peer's real reply arrives (or timeout).

    Synchronous receivers return the real reply in the HTTP response; for those
    this returns immediately with ``{"reply", "context_id", "waited": False}``.

    Async receivers (e.g. opencode) return a ``[queued]`` ack; this then polls
    the receiver's transcript jsonl for the final reply on the same
    ``context_id``, returning ``{"reply", "context_id", "waited": True}`` when it
    lands. On timeout it returns the original ack plus ``"waited": False`` and a
    ``"reason"``. Requires local filesystem access to the receiver's transcript
    (resolved from the peer's ``repo_path`` or the ``transcript_path`` arg);
    without one it cannot poll and returns the ack with a ``"reason"``.
    """
    try:
        entry = get_agent(agent_name)
    except KeyError as exc:
        raise FleetClientError(exc.args[0] if exc.args else "unknown agent") from exc

    # Capture the transcript offset BEFORE sending so a fast reply written
    # between the HTTP response and our first poll is not skipped.
    tpath = _resolve_transcript_path(entry, transcript_path)
    start_offset = 0
    if tpath is not None:
        try:
            start_offset = tpath.stat().st_size
        except OSError:
            start_offset = 0

    first = await send_message(
        agent_name, text, context_id=context_id, timeout=send_timeout, client=client
    )
    reply = first.get("reply", "")
    ctx = first.get("context_id") or context_id or ""

    if QUEUED_MARKER not in reply:
        return {**first, "waited": False}
    if tpath is None:
        return {
            **first,
            "waited": False,
            "reason": "queued ack but no transcript_path resolvable "
            "(peer entry has no repo_path; pass transcript_path=)",
        }
    if not ctx:
        return {**first, "waited": False, "reason": "queued ack had no context_id to correlate"}

    deadline = asyncio.get_event_loop().time() + max_wait
    offset = start_offset
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(poll_interval)
        found, offset = _scan_transcript_reply(tpath, ctx, offset)
        if found is not None:
            return {"reply": found, "context_id": ctx, "waited": True}
    return {
        **first,
        "waited": False,
        "reason": f"no final reply in transcript within {max_wait}s",
    }


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
        result = await send_message(agent_name, text)
    except FleetClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(result["reply"])
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain(sys.argv)))


if __name__ == "__main__":
    main()
