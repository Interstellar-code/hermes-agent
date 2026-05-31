"""Agent-facing tools for the a2a_fleet plugin.

v0.1 ships a single ``fleet_send`` tool that wraps :func:`client.send_message`
in a dict-returning shape so the calling agent never sees a raised exception.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from .client import FleetClientError, send_message

log = logging.getLogger("a2a_fleet.tools")


async def fleet_send_handler(
    agent: Any = "",
    message: str = "",
    context_id: str = "",
    **_injected: Any,  # absorb gateway-injected kwargs (e.g. task_id)
) -> Dict[str, Any]:
    """Send ``message`` to the named fleet peer and return ``{"reply": ..., "context_id": ...}``.

    ``context_id`` is optional; when empty the server generates one and the
    generated id is returned in the result so the caller can continue the thread.

    On any failure returns ``{"error": "..."}`` rather than raising — the calling
    agent can surface the string verbatim in chat without exception handling.

    Dispatch shape: ``registry.dispatch()`` calls ``handler(args, **kwargs)`` —
    the WHOLE args dict lands in the first positional (``agent``) and ``task_id``
    is injected as a kwarg. Unwrap that dict here (mirrors the cc handlers'
    ``canonicalize_repo_path`` unwrap) so the tool works on the live gateway path,
    while still tolerating direct kwarg-style calls (tests / internal callers).
    """
    if isinstance(agent, dict):
        _params = agent
        agent = _params.get("agent", "") or ""
        message = _params.get("message", "") or message
        context_id = _params.get("context_id", "") or context_id

    if not isinstance(agent, str) or not isinstance(message, str):
        return {"error": "fleet_send requires 'agent' and 'message' to be strings"}
    if not agent or not message:
        return {"error": "fleet_send requires both 'agent' and 'message'"}

    try:
        result = await send_message(
            agent,
            message,
            context_id=context_id if context_id else None,
        )
    except FleetClientError as exc:
        log.warning("fleet_send: peer %r returned an error: %s", agent, exc)
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — tools must never crash the agent loop
        log.exception("fleet_send: unexpected error talking to %r", agent)
        return {"error": f"unexpected error: {exc}"}
    return {"reply": result["reply"], "context_id": result["context_id"]}
