"""a2a_fleet — Hermes plugin enabling Agent-to-Agent communication.

v0.1: echo-only ping/pong over JSON-RPC. The plugin owns its own
uvicorn server on a dedicated A2A port (set in ``fleet.yaml``'s
``server.bind_port``) — fully isolated from the Hermes dashboard gateway.

See ``README.md`` for the full architecture and roadmap.
"""
from __future__ import annotations

import atexit
import functools
import importlib.util
import json
import logging
import threading

logger = logging.getLogger("a2a_fleet.plugin")


def _json_tool_result(fn):
    """Wrap an async tool handler so a dict/list return is JSON-stringified.

    Tool handlers in this plugin build structured dicts, but the gateway puts a
    handler's return value DIRECTLY into the tool-result message ``content``
    (see agent/tool_dispatch_helpers.make_tool_result_message). OpenAI-compatible
    upstreams require ``content`` to be a string — a raw dict serializes to an
    object and is rejected with ``invalid message content type:
    map[string]interface{}`` (HTTP 400). Stringify non-str returns here so the
    handlers can keep their dict API while the tool wire format stays valid.
    """
    @functools.wraps(fn)
    async def _wrapper(*args, **kwargs):
        result = await fn(*args, **kwargs)
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)
    return _wrapper

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
        atexit.register(_atexit_stop)
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
    from . import fleet_yaml_io  # noqa: WPS433 — lazy.

    # First-enable scaffold: on a fresh profile there is no fleet.yaml, so
    # load_fleet() below would raise FleetConfigError and the plugin would go
    # silently idle. Write a commented example (enabled, response_handler: agent,
    # server block, empty peers) if absent so the node actually comes up with
    # documented, editable config instead of a dead log line. Idempotent + never
    # raises (a read-only home just leaves it absent and we fall through to idle).
    try:
        fleet_yaml_io.ensure_example_fleet_yaml()
    except Exception:  # noqa: BLE001 — scaffolding must never break plugin load.
        logger.debug("a2a_fleet: example fleet.yaml scaffold failed", exc_info=True)

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
        handler=_json_tool_result(fleet_tools.fleet_send_handler),
        check_fn=None,
        is_async=True,
        description="Send a message to a fleet peer agent via A2A and return the reply.",
        emoji="🤝",
    )
    # v0.3 deploy/manage tools: stand up a Claude Code executor receiver in a
    # target repo. Lazy-import keeps the import cost off the hot path / avoids
    # cycles. Additive — never touches the fleet_send registration above.
    from . import agy_deploy  # noqa: WPS433 — lazy import is the contract.
    from . import cc_deploy  # noqa: WPS433 — lazy import is the contract.
    from . import codex_deploy  # noqa: WPS433 — lazy import is the contract.
    from . import oc_deploy  # noqa: WPS433 — lazy import is the contract.

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
                        "authorized in. Symlinked inputs are RESOLVED to their real "
                        "on-disk target and the receiver cwd is pinned there "
                        "(security preserved)."
                    ),
                },
                "bind_port": {
                    "type": "integer",
                    "description": "Port the receiver binds on. Omit to reuse this repo's existing port or auto-pick a free one in the claude_code band (9300-9309).",
                },
                "model": {
                    "type": "string",
                    "description": "Optional claude model to pin (e.g. 'sonnet', 'opus').",
                },
            },
            "required": ["repo_path"],
        },
        handler=_json_tool_result(cc_deploy.deploy_cc_receiver_handler),
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
        handler=_json_tool_result(cc_deploy.cc_receiver_status_handler),
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
        handler=_json_tool_result(cc_deploy.cc_receiver_stop_handler),
        check_fn=None,
        is_async=True,
        description="Stop the repo's Claude Code receiver via its PID file and remove the pidfile.",
        emoji="🛑",
    )
    ctx.register_tool(
        name="deploy_oc_receiver",
        toolset="a2a",
        schema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": (
                        "Absolute path to the target repo OpenCode is set up + "
                        "authorized in. Symlinked inputs are RESOLVED to their real "
                        "on-disk target and the receiver cwd is pinned there "
                        "(security preserved)."
                    ),
                },
                "bind_port": {
                    "type": "integer",
                    "description": "Port the receiver binds on. Omit to reuse this repo's existing port or auto-pick a free one in the opencode band (9310-9319).",
                },
                "model": {
                    "type": "string",
                    "description": "Optional OpenCode model to pin.",
                },
            },
            "required": ["repo_path"],
        },
        handler=_json_tool_result(oc_deploy.deploy_oc_receiver_handler),
        check_fn=None,
        is_async=True,
        description="Deploy + launch an OpenCode A2A executor receiver in a target repo.",
        emoji="🧩",
    )
    ctx.register_tool(
        name="oc_receiver_status",
        toolset="a2a",
        schema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the repo whose OpenCode receiver to check (PID + /health).",
                },
            },
            "required": ["repo_path"],
        },
        handler=_json_tool_result(oc_deploy.oc_receiver_status_handler),
        check_fn=None,
        is_async=True,
        description="Report whether the repo's OpenCode receiver is running (PID alive AND /health).",
        emoji="🩺",
    )
    ctx.register_tool(
        name="oc_receiver_stop",
        toolset="a2a",
        schema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the repo whose OpenCode receiver to stop (SIGTERM via PID file).",
                },
            },
            "required": ["repo_path"],
        },
        handler=_json_tool_result(oc_deploy.oc_receiver_stop_handler),
        check_fn=None,
        is_async=True,
        description="Stop the repo's OpenCode receiver via its PID file and remove the pidfile.",
        emoji="🛑",
    )
    ctx.register_tool(
        name="deploy_codex_receiver",
        toolset="a2a",
        schema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": (
                        "Absolute path to the target repo Codex CLI is set up + "
                        "authorized in. Symlinked inputs are RESOLVED to their real "
                        "on-disk target and the receiver cwd is pinned there "
                        "(security preserved)."
                    ),
                },
                "bind_port": {
                    "type": "integer",
                    "description": "Port the receiver binds on. Omit to reuse this repo's existing port or auto-pick a free one in the codex band (9320-9329).",
                },
                "model": {
                    "type": "string",
                    "description": "Optional Codex model to pin (e.g. 'o4-mini', 'o3').",
                },
                "sandbox": {
                    "type": "string",
                    "description": (
                        "Codex sandbox level: read-only | workspace-write | danger-full-access "
                        "(default workspace-write). Only applies on first turn; resume inherits."
                    ),
                },
                "hermes_auth_token_env": {
                    "type": "string",
                    "description": "Env var name holding the outbound bearer token for replies to Hermes.",
                },
            },
            "required": ["repo_path"],
        },
        handler=_json_tool_result(codex_deploy.deploy_codex_receiver_handler),
        check_fn=None,
        is_async=True,
        description="Deploy + launch a Codex CLI A2A executor receiver in a target repo.",
        emoji="🚀",
    )
    ctx.register_tool(
        name="codex_receiver_status",
        toolset="a2a",
        schema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the repo whose Codex receiver to check (PID + /health).",
                },
            },
            "required": ["repo_path"],
        },
        handler=_json_tool_result(codex_deploy.codex_receiver_status_handler),
        check_fn=None,
        is_async=True,
        description="Report whether the repo's Codex receiver is running (PID alive AND /health).",
        emoji="🩺",
    )
    ctx.register_tool(
        name="codex_receiver_stop",
        toolset="a2a",
        schema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the repo whose Codex receiver to stop (SIGTERM via PID file).",
                },
            },
            "required": ["repo_path"],
        },
        handler=_json_tool_result(codex_deploy.codex_receiver_stop_handler),
        check_fn=None,
        is_async=True,
        description="Stop the repo's Codex receiver via its PID file and remove the pidfile.",
        emoji="🛑",
    )
    ctx.register_tool(
        name="deploy_agy_receiver",
        toolset="a2a",
        schema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": (
                        "Absolute path to the target repo Antigravity CLI (agy) is "
                        "set up + signed in (macOS Keychain). Symlinked inputs are "
                        "RESOLVED to their real on-disk target and the receiver cwd "
                        "is pinned there (security preserved)."
                    ),
                },
                "bind_port": {
                    "type": "integer",
                    "description": "Port the receiver binds on. Omit to reuse this repo's existing port or auto-pick a free one in the agy band (9330-9339).",
                },
                "sandbox": {
                    "type": "boolean",
                    "description": (
                        "Pass agy's --sandbox toggle (terminal restrictions). Boolean, "
                        "default false. agy has NO model selection (no --model flag). "
                        "Requires an interactive `agy` sign-in once on this host."
                    ),
                },
                "hermes_auth_token_env": {
                    "type": "string",
                    "description": "Env var name holding the outbound bearer token for replies to Hermes.",
                },
            },
            "required": ["repo_path"],
        },
        handler=_json_tool_result(agy_deploy.deploy_agy_receiver_handler),
        check_fn=None,
        is_async=True,
        description="Deploy + launch a Google Antigravity CLI (agy) A2A executor receiver in a target repo.",
        emoji="🚀",
    )
    ctx.register_tool(
        name="agy_receiver_status",
        toolset="a2a",
        schema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the repo whose agy receiver to check (PID + /health).",
                },
            },
            "required": ["repo_path"],
        },
        handler=_json_tool_result(agy_deploy.agy_receiver_status_handler),
        check_fn=None,
        is_async=True,
        description="Report whether the repo's Antigravity CLI receiver is running (PID alive AND /health).",
        emoji="🩺",
    )
    ctx.register_tool(
        name="agy_receiver_stop",
        toolset="a2a",
        schema={
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the repo whose agy receiver to stop (SIGTERM via PID file).",
                },
            },
            "required": ["repo_path"],
        },
        handler=_json_tool_result(agy_deploy.agy_receiver_stop_handler),
        check_fn=None,
        is_async=True,
        description="Stop the repo's Antigravity CLI receiver via its PID file and remove the pidfile.",
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

    # ``register_platform`` exists on every PluginContext, so this attempt runs
    # in any context; it is a no-op / debug-logged failure outside the gateway.
    # The gateway is the process that later calls A2AFleetAdapter.connect().
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

    # v0.6 boot-reconcile: re-provision any managed Claude Code / OpenCode
    # receivers that are down (or whose inbound token this gateway lost on
    # restart). Runs on its own
    # daemon thread so it never blocks plugin load; a clean no-op when there are no
    # managed peers (the common case / fresh installs). Guarded so a failure here
    # never disrupts the tools / skill / platform above.
    try:
        if hasattr(cc_deploy, "reconcile_managed_receivers_in_thread"):
            cc_deploy.reconcile_managed_receivers_in_thread()
    except Exception:  # noqa: BLE001 — additive, must never break register().
        logger.debug("a2a_fleet: boot-reconcile spawn failed", exc_info=True)

    # NOTE: the A2A uvicorn listener is deliberately NOT started here. register()
    # runs in EVERY process that loads the plugin (gateway, CLI tool startup,
    # dashboard web tier); starting the listener here raced all of them to bind
    # fleet.server.bind_port, and a bridge-less winner (e.g. the dashboard
    # process) then answered inbound `agent` requests with "bridge not ready"
    # (proven at runtime). The listener now starts from A2AFleetAdapter.connect()
    # — which runs ONLY in the gateway/agent process, right where the Route B
    # bridge is wired — so listener + bridge are co-located by construction (#120).
    logger.info("a2a_fleet: registered fleet_send + deploy tools (A2A server starts on gateway platform connect)")
