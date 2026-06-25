"""workflow_status — get the current status of a workflow run."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

SCHEMA: Dict[str, Any] = {
    "name": "workflow_status",
    "description": "Get the current status and recent events for a workflow run.",
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The workflow run id returned by workflow_run.",
            },
        },
        "required": ["run_id"],
    },
}

_MAX_EVENTS = 50


def check() -> bool:
    """Always allow — read-only tool."""
    return True


async def handler(args: Dict[str, Any], **kwargs: Any) -> str:  # noqa: ARG001
    return json.dumps(await _handler_impl(args, **kwargs), ensure_ascii=False, default=str)


async def _handler_impl(args: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:  # noqa: ARG001
    run_id: str = args.get("run_id", "")
    from .._shared import get_engine  # noqa: PLC0415

    engine = get_engine()
    run = await engine.get_run(run_id)
    if run is None:
        return {"error": f"Run '{run_id}' not found.", "ok": False}

    # Fetch recent events (best-effort; engine may not expose list_events yet)
    events: list = []
    try:
        events = await engine.list_events(run_id=run_id, limit=_MAX_EVENTS)
    except (AttributeError, Exception):
        # Engine version without list_events, or transient error — events
        # silently degrade to [].  Acceptable for v0.1; add logging if needed.
        pass

    return {
        "run_id": run.get("id"),
        "status": run.get("status"),
        "workflow_id": run.get("workflow_id"),
        "current_phase": run.get("current_phase"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "error": run.get("error"),
        "events": events[:_MAX_EVENTS],
        "ok": True,
    }
