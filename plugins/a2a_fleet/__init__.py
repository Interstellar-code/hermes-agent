"""a2a_fleet — Hermes plugin enabling Agent-to-Agent communication.

v0.1: echo-only ping/pong over JSON-RPC. The plugin owns its own
uvicorn server on a dedicated A2A port (set in ``fleet.yaml``'s
``server.bind_port``) — fully isolated from the Hermes dashboard gateway.

See ``README.md`` for the full architecture and roadmap.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("a2a_fleet.plugin")


async def _start_server_safe() -> None:
    """Run start_server() with structured failure logging.

    Wrapping the coroutine in ``loop.create_task`` would otherwise swallow any
    exception silently — leaving the plugin "registered" while the A2A surface
    is dead. Log loudly so operators see the failure in the gateway log.
    """
    from . import server  # noqa: WPS433 — lazy import is the contract.

    try:
        info = await server.start_server()
    except Exception as exc:  # noqa: BLE001 — log loudly, don't crash the agent loop
        logger.error("a2a_fleet: server failed to start: %s", exc, exc_info=True)
        return
    logger.info("a2a_fleet: server started %s", info)


def _schedule_server_start() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "a2a_fleet: no running event loop at register() time; "
            "server will not auto-start. Call start_server() manually.",
        )
        return
    loop.create_task(_start_server_safe(), name="a2a_fleet.start_server")


async def _stop_server_safe() -> None:
    from . import server  # noqa: WPS433 — lazy.

    try:
        info = await server.stop_server()
    except Exception as exc:  # noqa: BLE001
        logger.error("a2a_fleet: server stop raised: %s", exc, exc_info=True)
        return
    logger.info("a2a_fleet: server stopped %s", info)


def _schedule_server_stop() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("a2a_fleet: no running event loop at disable() time; cannot stop server")
        return
    loop.create_task(_stop_server_safe(), name="a2a_fleet.stop_server")


def register(ctx) -> None:
    """Plugin entry — registers fleet_send and starts the embedded A2A server.

    Respects ``fleet.enabled: false`` in fleet.yaml: when disabled, no server
    is started and no tool is registered. This keeps profiles that have the
    plugin installed but do not intend to participate in a fleet quiet.
    """
    from . import fleet_config  # noqa: WPS433 — lazy.
    from . import fleet_tools  # noqa: WPS433 — lazy.

    try:
        cfg = fleet_config.load_fleet()
    except fleet_config.FleetConfigError as exc:
        logger.warning(
            "a2a_fleet: fleet.yaml not usable (%s); plugin idle for this profile.",
            exc,
        )
        return

    if not cfg.get("enabled", True):
        logger.info("a2a_fleet: fleet.enabled is false; skipping tool + server registration.")
        return

    ctx.register_tool(
        name="fleet_send",
        toolset="a2a",
        schema={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Name of the fleet peer (matches fleet.yaml).",
                },
                "message": {
                    "type": "string",
                    "description": "Plain-text message to send to the peer agent.",
                },
            },
            "required": ["agent", "message"],
        },
        handler=fleet_tools.fleet_send_handler,
        check_fn=None,
        is_async=True,
        description="Send a message to a fleet peer agent via A2A and return the reply.",
        emoji="🤝",
    )
    _schedule_server_start()
    logger.info("a2a_fleet: registered fleet_send tool + scheduled A2A server start")


def disable() -> None:
    """Plugin loader calls this on hot-reload / shutdown."""
    _schedule_server_stop()
    logger.info("a2a_fleet: disable() — server stop scheduled")
