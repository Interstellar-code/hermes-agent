"""Module-global bridge holder for the A2A Fleet platform adapter.

The A2A uvicorn server runs on a daemon thread with its own event loop.
The gateway runs on a different loop. ``A2AFleetAdapter.connect()`` stores
itself here so that server.py's agent dispatch path can call back into the
gateway loop without importing the adapter module directly (avoiding circular
imports).

Usage::

    from .agent_bridge import get_agent_bridge, set_agent_bridge
    bridge = get_agent_bridge()
    if bridge is None:
        raise A2ABridgeNotReady("...")
    reply = bridge.bridge_sync(text, context_id, peer_id, timeout)
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .adapter import A2AFleetAdapter


class A2ABridgeNotReady(RuntimeError):
    """Raised when the adapter bridge has not been connected yet."""


class A2ABusyError(RuntimeError):
    """Raised when a concurrent bridge_sync call is already in progress for the same context."""


_BRIDGE: Optional["A2AFleetAdapter"] = None


def set_agent_bridge(obj: Optional["A2AFleetAdapter"]) -> None:
    """Set (or clear) the global adapter bridge reference."""
    global _BRIDGE
    _BRIDGE = obj


def get_agent_bridge() -> Optional["A2AFleetAdapter"]:
    """Return the currently registered adapter bridge, or None."""
    return _BRIDGE
