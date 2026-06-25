"""
CronPoller — Phase 5

Polls Hermes Agent's cron API every POLL_INTERVAL_S seconds. For each job
with payload.switchui_workflow_id set, if due (last_run_at advanced beyond
last_fired_at stored in workflow_cron_jobs), calls engine.start_run.

Uses in-process import of cron.jobs.list_jobs to avoid HTTP overhead.
Falls back to httpx GET http://127.0.0.1:8642/api/cron/jobs on ImportError.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("workflow.cron-poller")

POLL_INTERVAL_S: float = 10.0
KANBAN_BASE_URL: str = "http://127.0.0.1:8642"


def _list_jobs_direct() -> List[Dict[str, Any]]:
    """Try to import list_jobs in-process (preferred path inside hermes-agent)."""
    from cron.jobs import list_jobs  # type: ignore[import]

    return list_jobs(include_disabled=False)


async def _list_jobs_http() -> List[Dict[str, Any]]:
    """HTTP fallback — used only when in-process import fails."""
    import httpx  # type: ignore[import]

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{KANBAN_BASE_URL}/api/cron/jobs")
        resp.raise_for_status()
        data = resp.json()
        # web_server returns {"jobs": [...]} or plain list
        if isinstance(data, list):
            return data
        return data.get("jobs", [])


async def _fetch_jobs() -> List[Dict[str, Any]]:
    """Fetch cron jobs, preferring in-process import."""
    try:
        return _list_jobs_direct()
    except Exception:
        return await _list_jobs_http()


def _get_last_fired_at(conn: Any, job_id: str) -> Optional[str]:
    """Read last_fired_at from workflow_cron_jobs table."""
    row = conn.execute(
        "SELECT last_fired_at FROM workflow_cron_jobs WHERE cron_job_id = ?",
        (job_id,),
    ).fetchone()
    return row["last_fired_at"] if row else None


def _upsert_last_fired_at(conn: Any, job_id: str, fired_at: str) -> None:
    """Insert or update last_fired_at for a cron job."""
    conn.execute(
        """
        INSERT INTO workflow_cron_jobs (cron_job_id, last_fired_at)
        VALUES (?, ?)
        ON CONFLICT(cron_job_id) DO UPDATE SET last_fired_at = excluded.last_fired_at
        """,
        (job_id, fired_at),
    )
    conn.commit()


class CronPoller:
    """
    Async poller that bridges Hermes Agent cron jobs to the workflow engine.

    Lifecycle::

        poller = CronPoller(engine)
        task = asyncio.create_task(poller.run_forever())
        # on shutdown:
        task.cancel()
    """

    def __init__(self, engine: Any, poll_interval_s: float = POLL_INTERVAL_S) -> None:
        self._engine = engine
        self._poll_interval_s = poll_interval_s

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        logger.info("cron poller started (interval=%.0fs)", self._poll_interval_s)
        try:
            while True:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("cron poller tick failed: %s", exc)
                await asyncio.sleep(self._poll_interval_s)
        except asyncio.CancelledError:
            logger.info("cron poller stopped")

    async def tick(self) -> None:
        """Single poll cycle — exposed for testing."""
        await self._tick()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        jobs = await _fetch_jobs()
        conn = self._engine._conn  # direct access to SQLite connection

        for job in jobs:
            try:
                await self._process_job(job, conn)
            except Exception as exc:
                logger.exception("cron poller: error processing job %s: %s", job.get("id"), exc)

    async def _process_job(self, job: Dict[str, Any], conn: Any) -> None:
        job_id: Optional[str] = job.get("id")
        if not job_id:
            return

        payload = job.get("payload") or {}
        # payload may be a JSON string
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        workflow_id: Optional[str] = payload.get("switchui_workflow_id")
        if not workflow_id:
            return

        last_run_at: Optional[str] = job.get("last_run_at")
        if not last_run_at:
            return

        # Skip if job did not succeed
        if job.get("last_status") == "error":
            _upsert_last_fired_at(conn, job_id, last_run_at)
            return

        # Idempotency check
        last_fired_at = _get_last_fired_at(conn, job_id)
        if last_fired_at == last_run_at:
            return  # already fired for this cron tick

        # Fire!
        inputs: Dict[str, Any] = payload.get("inputs") or payload.get("switchui_input") or {}
        trigger = {"kind": "cron", "cron_job_id": job_id}

        logger.info(
            "cron poller: firing workflow=%s job=%s last_run_at=%s",
            workflow_id,
            job_id,
            last_run_at,
        )
        try:
            await self._engine.start_run(workflow_id, inputs, trigger)
        except Exception as exc:
            logger.exception(
                "cron poller: start_run failed for workflow=%s job=%s: %s",
                workflow_id,
                job_id,
                exc,
            )
        finally:
            # Advance cursor even on failure to avoid infinite retry storms
            _upsert_last_fired_at(conn, job_id, last_run_at)
