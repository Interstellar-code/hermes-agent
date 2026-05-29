"""Agent-facing tools for the a2a_fleet plugin.

v0.1 ships a single ``fleet_send`` tool that wraps :func:`client.send_message`
in a dict-returning shape so the calling agent never sees a raised exception.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from .client import FleetClientError, send_message

log = logging.getLogger("a2a_fleet.tools")


async def fleet_send_handler(agent: str, message: str) -> Dict[str, Any]:
    """Send ``message`` to the named fleet peer and return ``{reply}``.

    On any failure (network error, peer 401, peer JSON-RPC error, etc.) returns
    ``{"error": "..."}`` rather than raising — the calling agent can surface the
    string verbatim in chat without needing exception handling.
    """
    try:
        reply = await send_message(agent, message)
    except FleetClientError as exc:
        log.warning("fleet_send: peer %r returned an error: %s", agent, exc)
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — tools must never crash the agent loop
        log.exception("fleet_send: unexpected error talking to %r", agent)
        return {"error": f"unexpected error: {exc}"}
    return {"reply": reply}
