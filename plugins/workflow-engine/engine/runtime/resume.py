"""
Resume policy — on plugin restart, mark in-flight runs as 'crashed'.

No auto-resume in v1. Mirrors the TS resume.test.ts policy, with a PID guard:
- On a genuine process restart (PID differs from the one persisted in
  schema_meta), any run with status IN ('pending', 'running') is marked failed
  with error='crashed: plugin restarted'.
- On an in-process plugin re-initialization (same PID — gateway session
  compression, tool-loop protection, new agent session), in-flight runs are
  LEFT untouched: their asyncio tasks are still alive and finalize themselves.
- Paused runs (awaiting approval) are LEFT untouched — they resume explicitly
  via the /approve endpoint.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.store.run_store import RunStore

logger = logging.getLogger("workflow.resume")


def mark_crashed_runs(run_store: "RunStore") -> int:
    """
    Mark genuinely-crashed pending/running workflow_runs as failed='crashed'.

    Passes the current process PID so the store can tell a real process
    restart from an in-process plugin re-initialization (gateway session
    compression, tool-loop protection, new agent session). Only the former
    is a real crash; the latter leaves live asyncio run tasks running and
    must not be marked failed (#49). Returns the count of rows updated.
    """
    count = run_store.mark_crashed_runs(boot_pid=os.getpid())
    if count:
        logger.warning(
            "resume: marked %d in-flight run(s) as crashed (no auto-resume in v1)",
            count,
        )
    return count
