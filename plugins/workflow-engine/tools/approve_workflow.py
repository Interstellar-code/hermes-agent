"""workflow_approve — approve or reject a paused approval node.

check_fn enforces:
  (a) caller's session must own the run, OR workflow.approve_any=true
  (b) run must not be in a terminal state
"""
from __future__ import annotations

import json
from typing import Any, Dict, Literal, Optional

SCHEMA: Dict[str, Any] = {
    "name": "workflow_approve",
    "description": (
        "Approve or reject a paused approval node in a workflow run. "
        "Only the session that started the run may approve it (unless workflow.approve_any=true)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The workflow run id.",
            },
            "node_id": {
                "type": "string",
                "description": "The node id of the approval node to act on.",
            },
            "decision": {
                "type": "string",
                "enum": ["approve", "reject"],
                "description": "Whether to approve or reject the node.",
            },
            "note": {
                "type": "string",
                "description": "Optional comment to record alongside the decision.",
            },
        },
        "required": ["run_id", "node_id", "decision"],
    },
}

_TERMINAL_STATES = {"completed", "failed", "cancelled"}


def _approve_any() -> bool:
    try:
        from hermes_cli.config import load_config  # noqa: PLC0415
        return bool(load_config().get("workflow", {}).get("approve_any", False))
    except Exception:
        return False


def check() -> bool:
    """Always return True — ownership check happens inside handler."""
    return True


async def handler(args: Dict[str, Any], **kwargs: Any) -> str:
    return json.dumps(await _handler_impl(args, **kwargs), ensure_ascii=False, default=str)


async def _handler_impl(args: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    run_id: str = args.get("run_id", "")
    node_id: str = args.get("node_id", "")
    decision: Literal["approve", "reject"] = args.get("decision", "approve")  # type: ignore[assignment]
    note: Optional[str] = args.get("note")
    _session_key: Optional[str] = kwargs.get("_session_key") or kwargs.get("session_key")
    from .._shared import get_engine  # noqa: PLC0415

    engine = get_engine()
    run = await engine.get_run(run_id)
    if run is None:
        return {"error": f"Run '{run_id}' not found.", "ok": False}

    # Reject terminal state
    status = run.get("status", "")
    if status in _TERMINAL_STATES:
        return {
            "error": f"Run '{run_id}' is in terminal state '{status}' and cannot be approved.",
            "ok": False,
        }

    # Ownership check
    owner_session = run.get("owner_session")
    if not _approve_any():
        if _session_key and owner_session and owner_session != _session_key:
            return {
                "error": (
                    f"Run '{run_id}' was started by a different session. "
                    "Set workflow.approve_any=true to allow cross-session approvals."
                ),
                "ok": False,
            }
        if _session_key and owner_session is None:
            # NULL owner — refuse unless approve_any
            return {
                "error": (
                    f"Run '{run_id}' has no recorded owner (pre-migration run). "
                    "Set workflow.approve_any=true to approve ownerless runs."
                ),
                "ok": False,
            }

    await engine.approve(run_id=run_id, node_id=node_id, decision=decision, comment=note)
    return {"ok": True, "run_id": run_id, "node_id": node_id, "decision": decision}
