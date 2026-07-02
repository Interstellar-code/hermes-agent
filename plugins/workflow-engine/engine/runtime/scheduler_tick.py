"""Scheduler tick — periodically fires due `scheduled_runs` rows.

Mirrors the CronPoller pattern: a long-running coroutine launched from
daemon.py alongside the cron poller. Single responsibility: ask the
engine to claim+fire any due deferred runs.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("workflow.scheduler-tick")

DEFAULT_INTERVAL_S: float = 10.0


async def run_scheduler_tick_loop(
    engine: Any, interval_s: float = DEFAULT_INTERVAL_S,
) -> None:
    """Run the scheduler-tick loop forever (until cancelled)."""
    logger.info("scheduler tick started (interval=%.0fs)", interval_s)
    try:
        while True:
            try:
                await engine.fire_due_scheduled_runs()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("scheduler tick failed: %s", exc)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        logger.info("scheduler tick stopped")
