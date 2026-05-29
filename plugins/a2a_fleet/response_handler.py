"""Response handler for incoming A2A SendMessage calls.

v0.1 ships a single ``echo_handler``. A ``ResponseHandler`` Protocol class is
deliberately omitted until v0.2 introduces a second handler (LLM-backed),
matching the plan's "no premature abstraction" rule.
"""
from __future__ import annotations


async def echo_handler(text: str, context_id: str) -> str:
    """Return ``pong`` for ``ping``; otherwise echo the input verbatim."""
    if text.strip().lower() == "ping":
        return "pong"
    return text
