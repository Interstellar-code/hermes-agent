"""
Routed prompt node — runs a prompt node as a gateway run on another profile.

A prompt node with ``hermes_task.profile`` is sent to
``POST <gateway_url>/p/<profile>/v1/runs`` and polled to a terminal status.
Routing is opt-in via ``workflow.routing`` in the home's config.yaml (read per
call, never cached); when disabled the node runs on the local prompt path.

Security: the profile must be allowlisted, the gateway URL must be loopback,
the client ignores proxy env and redirects, and the profile's API_SERVER_KEY
only ever travels in the Authorization header (never in events, logs, errors).

Error wording is chosen against classify_error(): policy failures contain
"forbidden" (FATAL, never retried); time-limit/approval/interrupted failures
avoid TRANSIENT words so the DAG does not re-dispatch a run that may still be
live. Gateway-down and 429-exhausted stay TRANSIENT — a DAG retry gets a fresh
idempotency nonce, which is safe because the earlier dispatch never started.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import httpx
import yaml

from engine.core.executor_shared import substitute_inputs, substitute_node_output_refs
from engine.nodes.prompt import execute_prompt_node
from engine.schemas.workflow_run import NodeOutput

logger = logging.getLogger("workflow.nodes.agent_session")

POLL_S = 2.0
DEFAULT_TIMEOUT_S = 1800
MAX_POLL_ERRORS = 30
RATE_LIMIT_BACKOFF_S = (1, 2, 4, 8, 16)
CONNECT_ATTEMPTS = 4  # first try + 3 with the same idempotency key
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TERMINAL_FAILED = {"failed", "cancelled", "interrupted"}

# Monkeypatchable in tests (no real sleeping / wall clock).
_sleep = asyncio.sleep
_monotonic = time.monotonic


def routing_config(home: Optional[Any]) -> Dict[str, Any]:
    """``workflow.routing`` from ``<home>/config.yaml`` (fallback: hermes home).

    Missing or malformed config reads as disabled."""
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    try:
        data = yaml.safe_load((Path(home) / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        data = None
    wf = data.get("workflow") if isinstance(data, dict) else None
    r = wf.get("routing") if isinstance(wf, dict) else None
    r = r if isinstance(r, dict) else {}
    allowed = r.get("allowed_profiles")
    try:
        approval_wait_s = float(r.get("approval_wait_s", 120))
    except (TypeError, ValueError):
        approval_wait_s = 120.0
    return {
        "enabled": r.get("enabled") is True,
        "allowed_profiles": [str(p) for p in allowed] if isinstance(allowed, list) else [],
        "gateway_url": str(r.get("gateway_url") or "http://127.0.0.1:8642"),
        "approval_wait_s": approval_wait_s,
    }


def _policy_error(profile: str, cfg: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """(error, api_key). Every error contains "forbidden" → FATAL."""
    url = urlsplit(cfg["gateway_url"])
    if url.scheme not in ("http", "https") or url.hostname not in LOOPBACK_HOSTS:
        return "routing forbidden: workflow.routing.gateway_url is misconfigured (must be a loopback http(s) URL)", None
    if profile not in cfg["allowed_profiles"]:
        return f"routing to profile '{profile}' forbidden: not in workflow.routing.allowed_profiles", None
    if not _PROFILE_RE.match(profile) or profile == "default":
        return f"routing to profile '{profile}' forbidden: invalid profile name", None
    from hermes_cli.profiles import get_profile_dir
    profile_dir = get_profile_dir(profile)
    if not profile_dir.is_dir():
        return f"routing to profile '{profile}' forbidden: profile does not exist", None
    from agent.secret_scope import build_profile_secret_scope
    key = build_profile_secret_scope(profile_dir).get("API_SERVER_KEY") or ""
    if len(key) < 16:
        return f"routing to profile '{profile}' forbidden: profile has no API_SERVER_KEY (>=16 chars)", None
    return None, key


async def execute_agent_session_node(
    node,
    node_outputs: Dict[str, NodeOutput],
    ctx,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> "NodeExecutionResult":
    from engine.core.dag_executor import NodeExecutionResult

    cfg = routing_config(getattr(ctx, "home", None))
    if not cfg["enabled"]:
        return await execute_prompt_node(node, node_outputs, ctx)

    node_start = _monotonic()
    profile = node.hermes_task.profile
    ctx.emit_event("node_started", {
        "run_id": ctx.run_id,
        "node_id": node.id,
        "node_type": "prompt",
    })

    usage: Optional[Dict[str, Any]] = None

    def fail(err: str) -> "NodeExecutionResult":
        logger.error("dag_node_failed node=%s profile=%s error=%s", node.id, profile, err)
        payload: Dict[str, Any] = {"run_id": ctx.run_id, "node_id": node.id, "error": err}
        if usage:
            payload["usage"] = usage
        ctx.emit_event("node_failed", payload)
        return NodeExecutionResult(state="failed", error=err)

    err, api_key = _policy_error(profile, cfg)
    if err:
        return fail(err)

    raw_prompt = getattr(node, "prompt", "") or ""
    final_prompt = substitute_inputs(
        substitute_node_output_refs(raw_prompt, node_outputs),
        (getattr(ctx, "workflow_vars", None) or {}).get("inputs") or {},
    )
    instructions = f"Workflow step '{node.id}' of run {ctx.run_id}. Working directory: {ctx.cwd}"
    if getattr(node, "systemPrompt", None):
        instructions += "\n\n" + node.systemPrompt

    base = f"{cfg['gateway_url'].rstrip('/')}/p/{profile}/v1/runs"
    # Nonce per dispatch: this dispatch's own retries replay, a DAG retry starts fresh.
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Idempotency-Key": f"wf:{ctx.run_id}:{node.id}:{uuid.uuid4().hex[:12]}",
    }
    timeout_s = node.hermes_task.timeout_s or DEFAULT_TIMEOUT_S
    gw_run_id: Optional[str] = None

    async with httpx.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False, timeout=10.0,
    ) as client:

        async def stop() -> None:
            with suppress(Exception):
                await asyncio.wait_for(
                    client.post(f"{base}/{gw_run_id}/stop", headers=headers), timeout=1.5,
                )

        try:
            # ── dispatch ───────────────────────────────────────────────────
            rate_limited = connect_failures = 0
            while True:
                try:
                    resp = await client.post(
                        base, headers=headers,
                        json={"input": final_prompt, "instructions": instructions},
                    )
                except (httpx.ConnectError, httpx.TimeoutException):
                    connect_failures += 1
                    if connect_failures >= CONNECT_ATTEMPTS:
                        return fail(f"routed run to '{profile}': gateway unreachable (connection refused)")
                    await _sleep(connect_failures)
                    continue
                if resp.status_code == 429:
                    if rate_limited >= len(RATE_LIMIT_BACKOFF_S):
                        return fail(f"routed run to '{profile}': gateway rate limit (429), concurrency exhausted")
                    try:
                        delay = min(float(resp.headers.get("Retry-After", "")), 30.0)
                    except ValueError:
                        delay = RATE_LIMIT_BACKOFF_S[rate_limited]
                    rate_limited += 1
                    await _sleep(delay)
                    continue
                break
            if resp.status_code == 409:
                return fail(f"routed run to '{profile}' rejected: idempotency conflict (HTTP 409)")
            if resp.status_code in (401, 403):
                return fail(f"routed run to '{profile}' rejected: unauthorized (HTTP {resp.status_code})")
            if resp.status_code != 202:
                return fail(f"routed run to '{profile}' rejected by gateway (HTTP {resp.status_code})")
            gw_run_id = str(resp.json().get("run_id") or "")
            if not gw_run_id:
                return fail(f"routed run to '{profile}': gateway returned no run_id")
            logger.info("dag_node_routed node=%s profile=%s gateway_run_id=%s", node.id, profile, gw_run_id)

            # ── poll ───────────────────────────────────────────────────────
            announced = False
            poll_errors = 0
            approval_since: Optional[float] = None
            while True:
                if _monotonic() - node_start > timeout_s:
                    await stop()
                    return fail(f"routed run on '{profile}' exceeded its time limit ({timeout_s}s)")
                try:
                    resp = await client.get(f"{base}/{gw_run_id}", headers=headers)
                except httpx.TransportError:
                    resp = None
                if resp is not None and resp.status_code == 404:
                    return fail(f"routed run vanished (profile '{profile}', gateway run {gw_run_id})")
                if resp is None or resp.status_code != 200:
                    poll_errors += 1
                    if poll_errors > MAX_POLL_ERRORS:
                        # Run may still be live: stop it, and word the error
                        # so the DAG does not re-dispatch a duplicate.
                        await stop()
                        return fail(f"routed run on '{profile}': lost contact with gateway")
                    await _sleep(POLL_S)
                    continue
                poll_errors = 0
                status = resp.json()
                if isinstance(status.get("usage"), dict):
                    usage = {**status["usage"], "cost_usd": None, "model": None, "provider": None}
                if not announced:
                    announced = True
                    ctx.emit_event("node_session_started", {
                        "run_id": ctx.run_id,
                        "node_id": node.id,
                        "profile": profile,
                        "session_id": status.get("session_id") or gw_run_id,
                        "gateway_run_id": gw_run_id,
                    })
                state = status.get("status")
                if state == "completed":
                    break
                if state == "interrupted":
                    return fail(f"routed run on '{profile}' interrupted (gateway restarted)")
                if state in _TERMINAL_FAILED:
                    detail = status.get("error") or state
                    return fail(f"routed run on '{profile}' {state}: {detail}")
                if state == "waiting_for_approval":
                    approval_since = approval_since if approval_since is not None else _monotonic()
                    if _monotonic() - approval_since > cfg["approval_wait_s"]:
                        await stop()
                        return fail(f"routed run on '{profile}' approval wait exceeded")
                else:
                    approval_since = None
                await _sleep(POLL_S)
        except asyncio.CancelledError:
            if gw_run_id:
                await stop()
            raise

    output_text = status.get("output") or ""
    duration_ms = int((_monotonic() - node_start) * 1000)
    logger.info("dag_node_completed node=%s duration_ms=%d", node.id, duration_ms)
    ctx.emit_event("node_completed", {
        "run_id": ctx.run_id,
        "node_id": node.id,
        "output": output_text,
        "duration_ms": duration_ms,
        "type": "prompt",
        "usage": usage,
    })
    return NodeExecutionResult(state="completed", output=output_text)
