"""workflow_run — start a workflow run.

check_fn enforces:
  (a) working_path must resolve under an allowed root
  (b) per-session run-rate cap (workflow.run_rate_per_session, default 5/min)
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from pathlib import Path
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
    raw = cfg.get("allowed_roots", ["~", os.environ.get("HERMES_HOME", "~/.hermes")])
    roots = []
    for r in raw:
        expanded = os.path.expandvars(os.path.expanduser(str(r)))
        roots.append(Path(expanded).resolve())
    return roots


def _rate_cap() -> int:
    return int(_get_config().get("run_rate_per_session", 5))


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


async def handler(args: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    """Start a workflow run.

    registry.dispatch passes (args_dict, **kwargs); extract params from args.
    _session_key may arrive via kwargs when the registry forwards session context.
    """
    id: str = args.get("id", "")  # noqa: A001
    working_path: Optional[str] = args.get("working_path")
    inputs: Optional[Dict[str, Any]] = args.get("inputs")
    conversation_id: Optional[str] = args.get("conversation_id")
    _session_key: Optional[str] = kwargs.get("_session_key") or kwargs.get("session_key")
    # working_path validation
    path_err = _check_working_path(working_path)
    if path_err:
        return {"error": path_err, "ok": False}

    # rate cap
    rate_err = _check_rate(_session_key)
    if rate_err:
        return {"error": rate_err, "ok": False}

    from .._shared import get_engine  # noqa: PLC0415

    engine = get_engine()

    trigger: Dict[str, Any] = {"type": "agent", "source": "workflow_run_tool"}
    if working_path:
        trigger["working_path"] = working_path
    if conversation_id:
        trigger["conversation_id"] = conversation_id

    run = await engine.start_run(
        workflow_id=id,
        inputs=inputs or {},
        trigger=trigger,
    )

    # Record rate-limit timestamp
    if _session_key:
        _rate_buckets[_session_key].append(time.time())

    # Patch owner_session onto the run row (best-effort; schema may be pre-migration)
    if _session_key:
        try:
            engine.conn.execute(
                "UPDATE workflow_runs SET owner_session=? WHERE id=?",
                (_session_key, run["id"]),
            )
            engine.conn.commit()
        except Exception:
            pass

    return {
        "run_id": run.get("id"),
        "status": run.get("status"),
        "ok": True,
    }
