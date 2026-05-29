"""
Resume policy — on plugin restart, mark in-flight runs as 'crashed'.

No auto-resume in v1. Mirrors the TS resume.test.ts policy:
- Any run with status IN ('pending', 'running') at boot time is marked failed
  with error='crashed: plugin restarted'.
- Paused runs (awaiting approval) are LEFT untouched — they resume explicitly
  via the /approve endpoint.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.store.run_store import RunStore

logger = logging.getLogger("workflow.resume")


def mark_crashed_runs(run_store: "RunStore") -> int:
    """
    Mark all pending/running workflow_runs as failed='crashed'.
    Returns the count of rows updated.
    """
    count = run_store.mark_crashed_runs()
    if count:
        logger.warning(
            "resume: marked %d in-flight run(s) as crashed (no auto-resume in v1)",
            count,
        )
    return count
