"""a2a_fleet — Hermes plugin enabling Agent-to-Agent communication.

v0.1: echo-only ping/pong over JSON-RPC. The plugin owns its own
uvicorn server on a dedicated A2A port (set in ``fleet.yaml``'s
``server.bind_port``) — fully isolated from the Hermes dashboard gateway.

See ``README.md`` for the full architecture and roadmap.
"""
from __future__ import annotations

import atexit
import importlib.util
import logging
import threading

logger = logging.getLogger("a2a_fleet.plugin")

# Daemon thread that owns the server event loop — kept as a module-level
# reference so that _start_server_in_thread() is idempotent.
_server_thread: threading.Thread | None = None
_server_thread_lock = threading.Lock()


def _server_dependencies_available() -> bool:
    """Return True when optional inbound A2A server dependencies are installed."""
    missing = [
        name
        for name in ("fastapi", "uvicorn")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        logger.warning(
            "a2a_fleet: inbound server disabled; missing optional dependencies: %s",
            ", ".join(missing),
        )
        return False
    return True


def _run_server_in_own_loop() -> None:
    """Entry point for the named daemon thread.

    Creates a fresh event loop (never touching the caller's loop) and runs
    the uvicorn server to completion.  The daemon flag means the OS reclaims
    the thread when the main process exits even if stop_server() was never
    called explicitly.
    """
    import asyncio

    from . import server  # noqa: WPS433 — lazy import is the contract.

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        info = loop.run_until_complete(server.start_server())
        logger.info("a2a_fleet: server started %s", info)
        # Keep the loop alive so the server task can continue running.
        loop.run_forever()
    except Exception as exc:  # noqa: BLE001 — log loudly, don't crash the agent process
        logger.error("a2a_fleet: server failed to start: %s", exc, exc_info=True)
    finally:
        loop.close()


def _start_server_in_thread() -> None:
    """Spawn the server on a named daemon thread with its own event loop.

    Safe to call from any context — with or without a running asyncio loop.
    Idempotent: a second call while the thread is alive is a no-op.
    """
    global _server_thread

    with _server_thread_lock:
        if _server_thread is not None and _server_thread.is_alive():
            logger.debug("a2a_fleet: server thread already running, skipping spawn")
            return
        _server_thread = threading.Thread(
            target=_run_server_in_own_loop,
            name="a2a_fleet.server",
            daemon=True,
        )
        _server_thread.start()
    logger.info("a2a_fleet: server thread spawned")


def _atexit_stop() -> None:
    """Best-effort graceful stop registered via atexit.

    The daemon thread will be reaped by the OS on process exit regardless,
    but this gives uvicorn a chance to flush any in-flight responses cleanly.
    """
    from . import server  # noqa: WPS433 — lazy.

    try:
        server.stop_server_sync()
    except Exception:  # noqa: BLE001
        pass  # atexit handlers must never raise


def register(ctx) -> None:
    """Plugin entry — registers fleet_send and starts the embedded A2A server.

    Respects ``fleet.enabled: false`` in fleet.yaml: when disabled, no server
    is started and no tool is registered. This keeps profiles that have the
    plugin installed but do not intend to participate in a fleet quiet.

    The server is started on a dedicated daemon thread with its own event loop
    so that register() works correctly whether called from a plain synchronous
    context (no running loop) or from inside a running asyncio loop.
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

    if not _server_dependencies_available():
        logger.warning(
            "a2a_fleet: plugin idle for this profile; install hermes-agent[web] "
            "to enable the embedded A2A server."
        )
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
                "context_id": {
                    "type": "string",
                    "description": (
                        "Optional conversation context id for multi-turn exchanges. "
                        "When omitted the server generates one and returns it; "
                        "pass the returned context_id on subsequent turns to continue the thread."
                    ),
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
    # v0.3 deploy/manage tools: stand up a Claude Code executor receiver in a
    # target repo. Lazy-import keeps the import cost off the hot path / avoids
    # cycles. Additive — never touches the fleet_send registration above.
    from . import cc_deploy  # noqa: WPS433 — lazy import is the contract.

    ctx.register_tool(
        name="deploy_cc_receiver",
        toolset="a2a",
        schema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": (
                        "Absolute path to the target repo Claude Code is set up + "
                        "authorized in. Canonicalized; symlink-escapes are rejected."
                    ),
                },
                "bind_port": {
                    "type": "integer",
                    "description": "Port the receiver binds on (default 9300).",
                },
                "model": {
                    "type": "string",
                    "description": "Optional claude model to pin (e.g. 'sonnet', 'opus').",
                },
            },
            "required": ["repo_path"],
        },
        handler=cc_deploy.deploy_cc_receiver_handler,
        check_fn=None,
        is_async=True,
        description="Deploy + launch a Claude Code A2A executor receiver in a target repo.",
        emoji="🚀",
    )
    ctx.register_tool(
        name="cc_receiver_status",
        toolset="a2a",
        schema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the repo whose receiver to check (PID + /health).",
                },
            },
            "required": ["repo_path"],
        },
        handler=cc_deploy.cc_receiver_status_handler,
        check_fn=None,
        is_async=True,
        description="Report whether the repo's Claude Code receiver is running (PID alive AND /health).",
        emoji="🩺",
    )
    ctx.register_tool(
        name="cc_receiver_stop",
        toolset="a2a",
        schema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the repo whose receiver to stop (SIGTERM via PID file).",
                },
            },
            "required": ["repo_path"],
        },
        handler=cc_deploy.cc_receiver_stop_handler,
        check_fn=None,
        is_async=True,
        description="Stop the repo's Claude Code receiver via its PID file and remove the pidfile.",
        emoji="🛑",
    )

    # Plugin-scoped skill: end-to-end fleet bring-up + ping/pong verification.
    # Resolvable as 'a2a_fleet:deploy-fleet' via explicit load only.
    if hasattr(ctx, "register_skill"):
        from pathlib import Path  # noqa: PLC0415,WPS433
        try:
            ctx.register_skill(
                name="deploy-fleet",
                path=Path(__file__).parent / "skills" / "deploy-fleet" / "SKILL.md",
                description="Bring up an A2A fleet node and test peer communication (config, tokens, server verify, ping/pong).",
            )
        except Exception:
            logger.debug("a2a_fleet: register_skill failed", exc_info=True)

    if hasattr(ctx, "register_platform"):
        from . import adapter as _adapter  # noqa: WPS433 — lazy import is the contract.
        try:
            ctx.register_platform(
                name="a2a_fleet",
                label="A2A Fleet",
                adapter_factory=lambda cfg: _adapter.A2AFleetAdapter(cfg),
                check_fn=lambda: True,
                emoji="🤝",
            )
            logger.info("a2a_fleet: registered platform adapter with gateway")
        except Exception:
            logger.debug("a2a_fleet: register_platform failed (non-gateway context)", exc_info=True)

    _start_server_in_thread()
    atexit.register(_atexit_stop)
    logger.info("a2a_fleet: registered fleet_send tool + spawned A2A server thread")
