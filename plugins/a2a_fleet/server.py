"""Embedded uvicorn A2A server for the a2a_fleet plugin.

v0.1 architecture: the plugin runs its OWN FastAPI/uvicorn instance on a
dedicated port (configured in fleet.yaml's ``server.bind_port``), fully
isolated from the Hermes dashboard gateway. This sidesteps the gateway's
session-token middleware, localhost-only CORS, and Host-header validation —
all of which block cross-machine peer access by design.

Routes:
* ``GET  /.well-known/agent-card.json`` — PUBLIC discovery (RFC 8615)
* ``POST /jsonrpc``                     — A2A JSON-RPC 2.0 SendMessage endpoint
* ``GET  /health``                      — diagnostic

Security note: ``auth_required`` defaults to ``True`` in fleet_config so that
newly-created profiles opt into bearer-token protection automatically.  Sending
a plaintext bearer token over a non-loopback HTTP connection is inadvisable;
operators binding to a non-loopback address should terminate TLS in front of
this server.  The ``/jsonrpc`` endpoint checks the token via a constant-time
HMAC comparison to resist timing attacks.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .agent_bridge import A2ABridgeNotReady, A2ABusyError, get_agent_bridge
from .context_store import generate_context_id
from .fleet_config import load_fleet
from .llm_handler import A2AHandlerError, llm_handler
from .response_handler import HandlerResult, echo_handler

# Immutable handler registry — looked up per request by response_handler config key.
HANDLERS = {"echo": echo_handler, "llm": llm_handler}

# Distinct JSON-RPC error code for "busy / duplicate dispatch, retry" so clients
# can branch on it instead of the generic -32000 server error (P1-5).
A2A_BUSY_CODE = -32001

log = logging.getLogger("a2a_fleet.server")


# ---------------------------------------------------------------------------
# Agent Card builder — inline, no separate module (plan: 80 LoC inline OK)
# ---------------------------------------------------------------------------

def _build_agent_card(cfg: Dict[str, Any]) -> Dict[str, Any]:
    self_block = cfg["self"]
    host = self_block["bind_host"]
    port = self_block["bind_port"]
    base = f"http://{host}:{port}"
    return {
        "name": self_block.get("name") or "a2a_fleet",
        "description": (
            "Hermes Agent profile exposed as an A2A v0.1 fleet member. "
            "Echo handler for ping/pong; TaskManager + SSE deferred to v0.2+."
        ),
        "url": f"{base}/jsonrpc",
        "version": "0.1.0",
        "protocolVersion": "1.0",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text", "text/plain"],
        "defaultOutputModes": ["text", "text/plain"],
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": (
                    "Pre-shared bearer token; clients supply the token "
                    "configured via fleet.yaml token_env."
                ),
            },
        },
        "security": [{"bearerAuth": []}],
        "skills": [
            {
                "id": "echo",
                "name": "Echo",
                "description": "Returns 'pong' for input 'ping'; otherwise echoes the input verbatim.",
                "tags": ["v0.1", "diagnostic"],
                "examples": ["ping"],
            },
        ],
    }


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

def _rpc_error(rpc_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}},
        status_code=200,
    )


def _extract_text(params: Dict[str, Any]) -> str:
    message = params.get("message") or {}
    for part in message.get("parts") or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            return part["text"]
    return ""


def _context_id(params: Dict[str, Any]) -> str:
    message = params.get("message") or {}
    ctx = message.get("contextId")
    if isinstance(ctx, str) and ctx:
        return ctx
    return generate_context_id()


def _check_bearer(request: Request, cfg: Dict[str, Any]) -> Optional[JSONResponse]:
    self_block = cfg["self"]
    if not self_block.get("auth_required"):
        return None
    expected = self_block.get("token")
    if not expected:
        # Misconfiguration: auth_required=true but token_env not set or the
        # env var is empty.  Return 503 with a generic message — do NOT leak
        # the configuration detail (token_env name) to the caller.
        log.error(
            "a2a_fleet: auth_required=true but no token resolved from token_env=%r; "
            "refusing request to avoid unauthenticated access",
            self_block.get("token_env"),
        )
        return JSONResponse(
            {"error": "service unavailable: authentication misconfigured"},
            status_code=503,
        )
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return JSONResponse({"error": "missing bearer token"}, status_code=401)
    presented = header.split(None, 1)[1].strip()
    if not hmac.compare_digest(presented.encode(), expected.encode()):
        return JSONResponse({"error": "invalid bearer token"}, status_code=401)
    return None


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------

def build_app() -> FastAPI:
    """Construct the FastAPI app. Config is re-read on each request so that
    fleet.yaml edits do not require a server restart for the response shape."""
    app = FastAPI(
        title="a2a_fleet",
        version="0.1.0",
        description="A2A v0.1 fleet plugin — echo handler over JSON-RPC.",
        openapi_url=None,  # don't expose interactive docs on a peer-facing surface
        docs_url=None,
        redoc_url=None,
    )

    # A2A peers are server-to-server; browsers are not A2A clients.
    # CORS middleware is intentionally omitted: wildcard CORS would be
    # misleading and unnecessary on this surface.

    @app.get("/.well-known/agent-card.json")
    async def agent_card() -> JSONResponse:
        # PUBLIC — no bearer required. Capability discovery must be anonymous.
        return JSONResponse(_build_agent_card(load_fleet()))

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        cfg = load_fleet()
        return {
            "ok": True,
            "version": "0.1.0",
            "peer_count": len(cfg["agents"]),
        }

    @app.post("/jsonrpc")
    async def jsonrpc(request: Request) -> JSONResponse:
        cfg = load_fleet()
        auth_err = _check_bearer(request, cfg)
        if auth_err is not None:
            return auth_err

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _rpc_error(None, -32700, "Parse error: invalid JSON body")

        if not isinstance(body, dict):
            return _rpc_error(None, -32600, "Invalid Request: top-level body must be an object")

        rpc_id = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}
        if not isinstance(params, dict):
            return _rpc_error(rpc_id, -32602, "Invalid params: expected object")

        if method in {"SendMessage", "message/send"}:
            text = _extract_text(params)
            context_id = _context_id(params)

            if cfg["response_handler"] == "agent":
                # Route B: dispatch into the real Hermes agent via the adapter bridge.
                bridge = get_agent_bridge()
                if bridge is None:
                    return _rpc_error(
                        rpc_id,
                        -32000,
                        "agent bridge not ready (is platforms.a2a_fleet.enabled set + gateway running?)",
                    )
                # Extract peer identity from A2A message metadata / user field.
                msg_block = params.get("message") or {}
                peer_id = (
                    msg_block.get("user")
                    or (msg_block.get("metadata") or {}).get("peer_id")
                    or "a2a-peer"
                )
                timeout = float(cfg.get("agent", {}).get("timeout_s", 120))
                try:
                    reply = await asyncio.get_event_loop().run_in_executor(
                        None,
                        bridge.bridge_sync,
                        text,
                        context_id,
                        peer_id,
                        timeout,
                    )
                except A2ABusyError as exc:
                    return _rpc_error(rpc_id, A2A_BUSY_CODE, f"peer busy on this context, retry: {exc}")
                except A2ABridgeNotReady as exc:
                    return _rpc_error(rpc_id, -32000, f"agent bridge not ready: {exc}")
                except TimeoutError as exc:
                    return _rpc_error(rpc_id, -32000, f"agent reply timed out after {timeout}s: {exc}")
                except Exception as exc:
                    return _rpc_error(rpc_id, -32000, f"Server error: {exc}")
                result = HandlerResult(text=reply, context_id=context_id)
            else:
                handler = HANDLERS.get(cfg["response_handler"], echo_handler)
                try:
                    result = await handler(text, context_id, cfg)
                except (A2AHandlerError, Exception) as exc:
                    return _rpc_error(rpc_id, -32000, f"Server error: {exc}")

            return JSONResponse({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "kind": result.kind,
                    "message": {
                        "role": "agent",
                        "parts": [{"text": result.text}],
                        "contextId": result.context_id,
                    },
                },
            })

        known_unimplemented = {
            "SendStreamingMessage",
            "message/stream",
            "tasks.get",
            "tasks.list",
            "tasks.cancel",
        }
        if method in known_unimplemented:
            return _rpc_error(
                rpc_id, -32601,
                f"Method {method!r} not implemented in v0.1 (deferred to v0.2+).",
            )
        return _rpc_error(rpc_id, -32601, f"Method not found: {method!r}")

    return app


# ---------------------------------------------------------------------------
# Uvicorn lifecycle — single-instance per process
# ---------------------------------------------------------------------------

_server_instance: Optional[uvicorn.Server] = None
_server_task: Optional[asyncio.Task] = None


class A2AServerStartError(RuntimeError):
    """Raised when the embedded uvicorn server fails to bind or crashes during startup."""


async def start_server(timeout: float = 5.0) -> Dict[str, Any]:
    """Boot the A2A uvicorn server in a background asyncio task.

    Idempotent: a second call while the server is running is a no-op.
    Reads bind_host / bind_port from fleet.yaml at start time.

    Raises ``A2AServerStartError`` if the server task crashes before binding
    (e.g. port in use) or fails to report ``started=True`` within ``timeout``.
    """
    global _server_instance, _server_task

    if _server_instance is not None and not _server_instance.should_exit:
        log.info("a2a_fleet: server already running, skipping start")
        return {
            "started": False,
            "host": _server_instance.config.host,
            "port": _server_instance.config.port,
            "already_running": True,
        }

    cfg = load_fleet()
    host = cfg["self"]["bind_host"]
    port = cfg["self"]["bind_port"]
    app = build_app()
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        lifespan="off",
    )
    _server_instance = uvicorn.Server(config)
    # uvicorn ignores signal handlers when run as a library — disable to
    # avoid stomping on the Hermes agent process's own handlers.
    _server_instance.install_signal_handlers = lambda: None  # type: ignore[assignment]

    # Wrap serve() so that uvicorn's ``sys.exit(1)`` on bind failure surfaces
    # as a regular exception on ``task.exception()`` rather than propagating
    # SystemExit through ``asyncio.run`` and the surrounding event loop.
    async def _serve() -> None:
        assert _server_instance is not None  # narrow type for mypy
        try:
            await _server_instance.serve()
        except SystemExit as exc:
            raise A2AServerStartError(
                f"uvicorn exited via SystemExit (code={exc.code})"
            ) from exc

    task = asyncio.create_task(_serve(), name="a2a_fleet.server")
    _server_task = task
    inst = _server_instance  # local handle — see note below

    # Poll for either the bind to succeed (started=True) or the task to die
    # with an exception (typical case: port already in use). Poll the LOCAL
    # ``task`` / ``inst`` handles, not the module globals: a concurrent start/stop
    # (seen in the test suite spinning servers up/down) can null ``_server_task`` /
    # ``_server_instance`` mid-loop, and ``None.done()`` / ``None.started`` would
    # crash this startup path.
    deadline_iters = max(1, int(timeout / 0.02))
    for _ in range(deadline_iters):
        if task.done():
            # Task exited before reporting started — surface the failure.
            exc = task.exception()
            _server_instance = None
            _server_task = None
            raise A2AServerStartError(
                f"uvicorn task exited during startup on {host}:{port}: {exc!r}"
            ) from exc
        if inst.started:
            break
        await asyncio.sleep(0.02)
    else:
        # Loop completed without break — startup timed out without the task
        # dying. Force the task to exit and surface the timeout.
        inst.should_exit = True
        task.cancel()
        _server_instance = None
        _server_task = None
        raise A2AServerStartError(
            f"uvicorn did not report started=True within {timeout}s on {host}:{port}"
        )

    log.info("a2a_fleet: server started on %s:%d", host, port)
    return {"started": True, "host": host, "port": port}


async def stop_server() -> Dict[str, Any]:
    """Gracefully stop the running A2A uvicorn instance, if any."""
    global _server_instance, _server_task

    if _server_instance is None:
        return {"stopped": False, "reason": "not running"}
    _server_instance.should_exit = True
    task = _server_task
    if task is not None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        same_loop = running is not None and task.get_loop() is running
        if same_loop:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("a2a_fleet: server did not exit in 5s, cancelling task")
                task.cancel()
        else:
            # We are being called from a different loop than the one that
            # started the server. Best-effort cancel; the original loop owns
            # the task lifecycle.
            log.warning(
                "a2a_fleet: stop_server invoked from foreign loop; cancelling task only",
            )
            try:
                task.cancel()
            except Exception:  # noqa: BLE001
                pass
    _server_instance = None
    _server_task = None
    log.info("a2a_fleet: server stopped")
    return {"stopped": True}


def is_running() -> bool:
    return _server_instance is not None and not _server_instance.should_exit


def stop_server_sync() -> None:
    """Synchronous best-effort stop for use from atexit handlers and daemon threads.

    Signals uvicorn to exit without awaiting task completion — the daemon
    thread's event loop will drain naturally as the process shuts down.
    """
    global _server_instance, _server_task

    if _server_instance is not None:
        _server_instance.should_exit = True
        log.info("a2a_fleet: stop_server_sync: signalled server to exit")
    _server_instance = None
    _server_task = None
