"""workflow_run — start a workflow run.

check_fn enforces:
  (a) working_path must resolve under an allowed root
  (b) per-session run-rate cap (workflow.run_rate_per_session, default 5/min)
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path

from hermes_constants import get_hermes_home
from typing import Any, Dict, List, Optional

SCHEMA: Dict[str, Any] = {
    "name": "workflow_run",
    "description": (
        "Start a workflow run. Requires the workflow definition id. "
        "working_path must resolve under an allowed root configured in workflow.allowed_roots."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Workflow definition id (e.g. 'archon-fix-github-issue').",
            },
            "working_path": {
                "type": "string",
                "description": "Absolute path to the working directory for the run.",
            },
            "inputs": {
                "type": "object",
                "description": "Arbitrary key/value inputs passed to the workflow.",
            },
            "conversation_id": {
                "type": "string",
                "description": "Conversation id to associate with the run (optional).",
            },
        },
        "required": ["id"],
    },
}

# Per-session rate limiting: (session_key) -> list of epoch timestamps
_rate_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW_S = 60.0


def _get_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config  # noqa: PLC0415
        return load_config().get("workflow", {})
    except Exception:
        return {}


def _allowed_roots() -> List[Path]:
    cfg = _get_config()
    raw = cfg.get("allowed_roots", ["~", str(get_hermes_home())])
    roots = []
    for r in raw:
        expanded = os.path.expandvars(os.path.expanduser(str(r)))
        roots.append(Path(expanded).resolve())
    return roots


def _rate_cap() -> int:
    return int(_get_config().get("run_rate_per_session", 5))


def _wait_timeout_s() -> Optional[float]:
    """Seconds to block the tool waiting for the run to settle.

    Settles == completed / failed / cancelled / paused. Configurable
    via ``workflow.run_wait_timeout_seconds`` in config.yaml so long
    workflows aren't artificially capped. ``0`` or negative disables
    the wait entirely (legacy fire-and-forget — caller must poll).
    Default 300s (5 min) — long enough for typical agent-triggered
    workflows, short enough that a stuck DAG surfaces to the agent
    as a still-running response rather than hanging the conversation.
    """
    raw = _get_config().get("run_wait_timeout_seconds", 300)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 300.0
    return v if v > 0 else None


def check() -> bool:
    """Always return True — fine-grained auth happens inside handler via _check_request."""
    return True


def _check_working_path(working_path: Optional[str]) -> Optional[str]:
    """Return error string if working_path is invalid, else None."""
    if working_path is None:
        return None
    try:
        p = Path(working_path).resolve()
    except Exception:
        return f"working_path '{working_path}' could not be resolved"

    if ".." in Path(working_path).parts:
        return "working_path must not contain '..' segments"

    roots = _allowed_roots()
    for root in roots:
        try:
            p.relative_to(root)
            return None  # allowed
        except ValueError:
            continue

    return (
        f"working_path '{working_path}' is not under any allowed root "
        f"({', '.join(str(r) for r in roots)}). "
        "Configure workflow.allowed_roots to expand the allowlist."
    )


def _check_rate(session_key: Optional[str]) -> Optional[str]:
    if not session_key:
        return None
    cap = _rate_cap()
    now = time.time()
    bucket = _rate_buckets[session_key]
    # evict timestamps outside the window
    _rate_buckets[session_key] = [t for t in bucket if now - t < _RATE_WINDOW_S]
    if len(_rate_buckets[session_key]) >= cap:
        return (
            f"Rate limit exceeded: {cap} workflow_run calls per {int(_RATE_WINDOW_S)}s per session. "
            "Wait before starting another run."
        )
    return None


async def handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """Start a workflow run.

    registry.dispatch passes (args_dict, **kwargs); extract params from args.
    _session_key may arrive via kwargs when the registry forwards session context.
    """
    return json.dumps(await _handler_impl(args, **kwargs), ensure_ascii=False, default=str)


async def _handler_impl(args: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    id: str = args.get("id", "")  # noqa: A001
    working_path: Optional[str] = args.get("working_path")
    inputs: Optional[Dict[str, Any]] = args.get("inputs")
    conversation_id: Optional[str] = args.get("conversation_id")
    _session_key: Optional[str] = kwargs.get("_session_key") or kwargs.get("session_key")
    # working_path validation
    path_err = _check_working_path(working_path)
    if path_err:
        return {"error": path_err, "ok": False}

    # rate cap — reserve the slot *before* the await to prevent TOCTOU races
    rate_err = _check_rate(_session_key)
    if rate_err:
        return {"error": rate_err, "ok": False}

    # Append timestamp now (slot reserved); roll back on failure below.
    reserved_ts: Optional[float] = None
    if _session_key:
        reserved_ts = time.time()
        _rate_buckets[_session_key].append(reserved_ts)

    from .._shared import get_engine  # noqa: PLC0415

    engine = get_engine()

    trigger: Dict[str, Any] = {"type": "agent", "source": "workflow_run_tool"}
    if working_path:
        trigger["working_path"] = working_path
    if conversation_id:
        trigger["conversation_id"] = conversation_id

    try:
        run = await engine.start_run(
            workflow_id=id,
            inputs=inputs or {},
            trigger=trigger,
        )
    except Exception:
        # Roll back the reserved slot so a transient failure doesn't burn a rate token.
        if _session_key and reserved_ts is not None:
            try:
                _rate_buckets[_session_key].remove(reserved_ts)
            except ValueError:
                pass
        raise

    run_id = run.get("id")

    # Patch owner_session onto the run row (best-effort; schema may be pre-migration)
    if _session_key and run_id:
        try:
            engine.conn.execute(
                "UPDATE workflow_runs SET owner_session=? WHERE id=?",
                (_session_key, run_id),
            )
            engine.conn.commit()
        except Exception:
            pass

    # Block until the run settles. Without this the runner's background
    # asyncio task is orphaned the instant we return — the agent's
    # conversation loop stops pumping after the tool handler completes,
    # so the bash subprocess inside _execute never gets CPU time and
    # the run sits forever in 'running' (Interstellar-code#2).
    #
    # wait_for_run treats 'paused' as settled so approval-gate
    # workflows still come back promptly; the agent gets the run_id
    # and can poll / approve out-of-band later.
    wait_s = _wait_timeout_s()
    final: Optional[Dict[str, Any]] = run
    waited = False
    if wait_s is not None and run_id:
        # wait_for_run swallows TimeoutError + downstream exceptions
        # itself (see facade.wait_for_run + runner.wait_for); the only
        # propagated exception is CancelledError, which we let through
        # to the agent loop intentionally.
        final = await engine.wait_for_run(run_id, timeout=wait_s) or run
        waited = True

    status = (final or run).get("status")
    # Surface the unsettled case so the agent knows to poll
    # ``workflow_status`` instead of assuming the run failed.
    wait_timed_out = waited and status == "running"
    result: Dict[str, Any] = {
        "run_id": run_id,
        "status": status,
        "ok": True,
    }
    if wait_timed_out:
        result["wait_timed_out"] = True
        result["hint"] = (
            f"Workflow still running after {wait_s:.0f}s; poll workflow_status "
            f"with run_id={run_id} for progress."
        )
    return result
