"""Scheduler tick — periodically fires due `scheduled_runs` rows.

Mirrors the CronPoller pattern: a long-running coroutine launched from
daemon.py alongside the cron poller. Single responsibility: ask the
engine to claim+fire any due deferred runs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger("workflow.scheduler-tick")

DEFAULT_INTERVAL_S: float = 10.0
HEARTBEAT_FILENAME = "workflow-daemon.heartbeat"


def heartbeat_path(home: Any) -> Path:
    return Path(home) / HEARTBEAT_FILENAME


def profile_for_dir(directory: Any) -> str:
    """Profile name for a profile-home dir: ``profiles/<name>`` -> name, else default."""
    d = Path(directory)
    return d.name if d.parent.name == "profiles" else "default"


def write_heartbeat(path: Path, interval_s: float) -> None:
    """Atomically record "daemon alive now" (epoch ms + tick interval)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"at": int(time.time() * 1000), "interval_s": interval_s, "pid": os.getpid()}),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def read_heartbeat(path: Path) -> Tuple[bool, Optional[int]]:
    """Return (alive, heartbeat_at_ms). Alive = younger than 3x the tick interval."""
    try:
        hb = json.loads(path.read_text(encoding="utf-8"))
        at = int(hb["at"])
        interval = float(hb.get("interval_s") or DEFAULT_INTERVAL_S)
    except (OSError, ValueError, KeyError, TypeError):
        return False, None
    return (time.time() * 1000 - at) < 3 * interval * 1000, at


def _beat(heartbeat_file: Optional[Path], interval_s: float) -> None:
    if heartbeat_file is None:
        return
    try:
        write_heartbeat(heartbeat_file, interval_s)
    except OSError as exc:
        logger.warning("heartbeat write failed: %s", exc)


async def run_scheduler_tick_loop(
    engine: Any,
    interval_s: float = DEFAULT_INTERVAL_S,
    heartbeat_file: Optional[Path] = None,
) -> None:
    """Run the scheduler-tick loop forever (until cancelled).

    Heartbeat is written before and after each tick; the interval recorded in it
    is what /health uses for staleness. The daemon intentionally uses the default
    tick interval, not ``--interval`` (that one paces the cron poller).
    """
    logger.info("scheduler tick started (interval=%.0fs)", interval_s)
    try:
        while True:
            _beat(heartbeat_file, interval_s)
            try:
                await engine.fire_due_scheduled_runs()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("scheduler tick failed: %s", exc)
            _beat(heartbeat_file, interval_s)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        logger.info("scheduler tick stopped")
