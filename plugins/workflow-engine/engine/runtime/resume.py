"""
Resume policy — fail in-flight runs whose owning process is gone.

No auto-resume in v1. Ownership is proven by heartbeat: every engine
refreshes ``last_heartbeat`` of the runs it is executing every
``HEARTBEAT_S`` (WorkflowRunner.heartbeat_forever). A pending/running run
whose heartbeat is older than ``STALE_MS`` lost its owner and is marked
failed. Runs live in another process (gateway vs daemon vs dashboard) keep
fresh heartbeats and are left alone — a single global boot PID used to fail
them all (L2-02). Paused runs are never touched; they resume via approve.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.store.run_store import RunStore

logger = logging.getLogger("workflow.resume")


def mark_crashed_runs(run_store: "RunStore") -> int:
    """Mark stale-heartbeat pending/running runs failed. Returns the count."""
    count = run_store.mark_crashed_runs()
    if count:
        logger.warning(
            "resume: marked %d orphaned run(s) as crashed (no auto-resume in v1)",
            count,
        )
    return count
