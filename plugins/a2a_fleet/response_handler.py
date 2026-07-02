"""Response handler for incoming A2A SendMessage calls.

v0.1 ships a single ``echo_handler``. A ``ResponseHandler`` Protocol class is
deliberately omitted until v0.2 introduces a second handler (LLM-backed),
matching the plan's "no premature abstraction" rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HandlerResult:
    """Internal result type returned by all inbound A2A handlers.

    ``kind`` is reserved for future use (e.g. ``"task"`` in the async phase).
    ``Turn`` shape and task fields are intentionally omitted until needed.
    """

    text: str
    context_id: str
    kind: str = field(default="message")


async def echo_handler(text: str, context_id: str, cfg: dict | None = None) -> HandlerResult:
    """Return ``pong`` for ``ping``; otherwise echo the input verbatim."""
    if text.strip().lower() == "ping":
        reply = "pong"
    else:
        reply = text
    return HandlerResult(text=reply, context_id=context_id)
