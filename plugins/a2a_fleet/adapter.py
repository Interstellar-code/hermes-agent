"""A2A Fleet platform adapter — bridges inbound A2A messages into the Hermes gateway.

When ``response_handler: agent`` is configured in fleet.yaml, this adapter is
registered as a gateway platform so that inbound SendMessage calls are
dispatched into the real Hermes agent (its conversation loop, SOUL, tools,
memory) and the agent's reply is returned synchronously to the A2A peer.

Design: the uvicorn A2A server runs on a daemon thread with its own event loop.
The gateway runs on a different loop.  ``bridge_sync()`` is called from the
uvicorn worker thread; it submits a coroutine to the gateway loop via
``asyncio.run_coroutine_threadsafe`` and blocks until the agent replies.

Per-context locking ensures that two concurrent A2A SendMessage requests for
the same contextId are serialised: the second caller gets an A2ABusyError
instead of racing with the first.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

def _a2a_fleet_adapter_factory(cfg: PlatformConfig) -> "A2AFleetAdapter":
    return A2AFleetAdapter(cfg)


# Self-register so that Platform("a2a_fleet") resolves via _missing_() without
# requiring the plugin to live under plugins/platforms/ (the filesystem scan path).
platform_registry.register(
    PlatformEntry(
        name="a2a_fleet",
        label="A2A Fleet",
        adapter_factory=_a2a_fleet_adapter_factory,
        check_fn=lambda: True,
        source="plugin",
        plugin_name="a2a_fleet",
        emoji="🤝",
    )
)

from .agent_bridge import (
    A2ABridgeNotReady,
    A2ABusyError,
    get_agent_bridge,
    set_agent_bridge,
)

log = logging.getLogger("a2a_fleet.adapter")


class A2AFleetAdapter(BasePlatformAdapter):
    """Platform adapter that routes inbound A2A messages into the Hermes agent.

    Lifecycle:
    1. ``connect()`` is awaited by the gateway on the gateway event loop.  It
       captures that loop and registers itself as the global bridge.
    2. The A2A uvicorn server's handler calls ``bridge_sync()`` from a worker
       thread to dispatch a message into the agent and obtain the reply.
    3. ``disconnect()`` clears the global bridge reference.

    The ``send()`` method is a no-op success because A2A replies are returned
    synchronously over the HTTP request — the gateway never needs to *push* a
    reply via the adapter's send channel.
    """

    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config, Platform("a2a_fleet"))
        self._gateway_loop: Optional[asyncio.AbstractEventLoop] = None
        # Per-context threading locks to serialise concurrent bridge_sync calls.
        self._ctx_locks: Dict[str, threading.Lock] = {}
        self._ctx_locks_guard = threading.Lock()

    # ------------------------------------------------------------------
    # BasePlatformAdapter abstract contract
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Capture the gateway event loop and register as the global bridge."""
        self._gateway_loop = asyncio.get_running_loop()
        set_agent_bridge(self)
        log.info("a2a_fleet: adapter connected; bridge ready")
        return True

    async def disconnect(self) -> None:
        """Deregister the global bridge reference."""
        set_agent_bridge(None)
        self._gateway_loop = None
        log.info("a2a_fleet: adapter disconnected; bridge cleared")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """No-op — A2A replies are returned synchronously, not pushed."""
        return SendResult(success=True, message_id=None)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm", "chat_id": chat_id}

    # ------------------------------------------------------------------
    # Synchronous bridge — called from the uvicorn worker thread
    # ------------------------------------------------------------------

    def bridge_sync(
        self,
        text: str,
        context_id: str,
        peer_id: str,
        timeout: float,
    ) -> str:
        """Dispatch ``text`` into the Hermes agent and return the reply.

        This method is SYNCHRONOUS and MUST be called from a worker thread,
        never from the uvicorn or gateway event loops directly.

        Args:
            text: The inbound message text from the A2A peer.
            context_id: A2A contextId — maps to the Hermes session chat_id.
            peer_id: Peer identity string used as user_id / user_name.
            timeout: Seconds to wait for the agent reply before raising
                ``TimeoutError``.

        Returns:
            The agent's reply as a plain string.

        Raises:
            A2ABusyError: A concurrent bridge_sync for the same context_id is
                already in progress.
            A2ABridgeNotReady: The adapter is not connected (gateway loop not
                available or message handler not wired).
            TimeoutError: The agent did not reply within ``timeout`` seconds.
        """
        # Serialise concurrent calls on the same context.
        with self._ctx_locks_guard:
            lock = self._ctx_locks.setdefault(context_id, threading.Lock())

        if not lock.acquire(blocking=False):
            raise A2ABusyError(
                f"A2A context {context_id!r} is already being processed; retry after the current turn completes."
            )
        try:
            if self._gateway_loop is None or self._message_handler is None:
                raise A2ABridgeNotReady(
                    "a2a_fleet adapter is not connected — "
                    "ensure platforms.a2a_fleet.enabled=true in the active profile config."
                )

            source = self.build_source(
                chat_id=context_id,
                chat_type="dm",
                user_id=peer_id or "a2a-peer",
                user_name=peer_id or "a2a-peer",
            )
            event = MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                internal=True,  # bypass gateway user-auth; A2A bearer is the gate
            )
            fut = asyncio.run_coroutine_threadsafe(
                self._message_handler(event),
                self._gateway_loop,
            )
            result = fut.result(timeout=timeout)
            return result or ""
        finally:
            lock.release()
