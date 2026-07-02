"""
Tests for engine/cron/poller.py

Uses a minimal fake engine + in-memory SQLite to exercise the poller without
a real Hermes Agent cron service.
"""
from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.cron.poller import CronPoller, _get_last_fired_at, _upsert_last_fired_at


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn() -> sqlite3.Connection:
    """Return an in-memory SQLite connection with the workflow_cron_jobs table."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE workflow_cron_jobs (
            cron_job_id TEXT PRIMARY KEY,
            last_fired_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def _make_engine(conn: sqlite3.Connection, start_run_result: Optional[Dict] = None) -> MagicMock:
    engine = MagicMock()
    engine._conn = conn
    engine.start_run = AsyncMock(return_value=start_run_result or {"id": "run-1"})
    return engine


def _make_job(
    job_id: str = "job-1",
    workflow_id: str = "my-workflow",
    last_run_at: str = "2026-05-19T10:00:00Z",
    last_status: str = "success",
    inputs: Optional[Dict] = None,
) -> Dict[str, Any]:
    return {
        "id": job_id,
        "name": "test-job",
        "enabled": True,
        "payload": {
            "switchui_workflow_id": workflow_id,
            "inputs": inputs or {},
        },
        "last_run_at": last_run_at,
        "last_status": last_status,
    }


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poller_fires_due_job():
    """Poller calls start_run for a job that has not been fired yet."""
    conn = _make_conn()
    engine = _make_engine(conn)
    poller = CronPoller(engine)

    jobs = [_make_job()]
    with patch("engine.cron.poller._fetch_jobs", new=AsyncMock(return_value=jobs)):
        await poller.tick()

    engine.start_run.assert_awaited_once_with(
        "my-workflow",
        {},
        {"kind": "cron", "cron_job_id": "job-1"},
    )
    # Cursor should be advanced
    assert _get_last_fired_at(conn, "job-1") == "2026-05-19T10:00:00Z"


@pytest.mark.asyncio
async def test_poller_skips_already_fired_job():
    """Poller does NOT call start_run if last_fired_at == last_run_at."""
    conn = _make_conn()
    _upsert_last_fired_at(conn, "job-1", "2026-05-19T10:00:00Z")
    engine = _make_engine(conn)
    poller = CronPoller(engine)

    jobs = [_make_job(last_run_at="2026-05-19T10:00:00Z")]
    with patch("engine.cron.poller._fetch_jobs", new=AsyncMock(return_value=jobs)):
        await poller.tick()

    engine.start_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_poller_skips_job_without_switchui_workflow_id():
    """Jobs without switchui_workflow_id are ignored."""
    conn = _make_conn()
    engine = _make_engine(conn)
    poller = CronPoller(engine)

    jobs = [{"id": "job-2", "payload": {"other_key": "val"}, "last_run_at": "2026-05-19T10:00:00Z", "last_status": "success"}]
    with patch("engine.cron.poller._fetch_jobs", new=AsyncMock(return_value=jobs)):
        await poller.tick()

    engine.start_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_poller_skips_errored_job_and_advances_cursor():
    """Jobs with last_status=error do not fire but cursor advances."""
    conn = _make_conn()
    engine = _make_engine(conn)
    poller = CronPoller(engine)

    jobs = [_make_job(last_status="error")]
    with patch("engine.cron.poller._fetch_jobs", new=AsyncMock(return_value=jobs)):
        await poller.tick()

    engine.start_run.assert_not_awaited()
    assert _get_last_fired_at(conn, "job-1") == "2026-05-19T10:00:00Z"


@pytest.mark.asyncio
async def test_poller_advances_cursor_on_start_run_failure():
    """Cursor advances even when start_run raises, preventing infinite retry."""
    conn = _make_conn()
    engine = _make_engine(conn)
    engine.start_run = AsyncMock(side_effect=RuntimeError("boom"))
    poller = CronPoller(engine)

    jobs = [_make_job()]
    with patch("engine.cron.poller._fetch_jobs", new=AsyncMock(return_value=jobs)):
        await poller.tick()

    assert _get_last_fired_at(conn, "job-1") == "2026-05-19T10:00:00Z"


@pytest.mark.asyncio
async def test_poller_passes_inputs():
    """Inputs from payload are forwarded to start_run."""
    conn = _make_conn()
    engine = _make_engine(conn)
    poller = CronPoller(engine)

    jobs = [_make_job(inputs={"repo": "my/repo", "days": 7})]
    with patch("engine.cron.poller._fetch_jobs", new=AsyncMock(return_value=jobs)):
        await poller.tick()

    engine.start_run.assert_awaited_once_with(
        "my-workflow",
        {"repo": "my/repo", "days": 7},
        {"kind": "cron", "cron_job_id": "job-1"},
    )


@pytest.mark.asyncio
async def test_poller_fires_new_run_after_cursor_advances():
    """Second tick with a new last_run_at fires a second run."""
    conn = _make_conn()
    engine = _make_engine(conn)
    poller = CronPoller(engine)

    job_v1 = _make_job(last_run_at="2026-05-19T10:00:00Z")
    job_v2 = _make_job(last_run_at="2026-05-19T10:10:00Z")

    with patch("engine.cron.poller._fetch_jobs", new=AsyncMock(return_value=[job_v1])):
        await poller.tick()

    with patch("engine.cron.poller._fetch_jobs", new=AsyncMock(return_value=[job_v2])):
        await poller.tick()

    assert engine.start_run.await_count == 2


@pytest.mark.asyncio
async def test_poller_json_string_payload():
    """Payload stored as JSON string is decoded correctly."""
    import json

    conn = _make_conn()
    engine = _make_engine(conn)
    poller = CronPoller(engine)

    job = {
        "id": "job-3",
        "last_run_at": "2026-05-19T10:00:00Z",
        "last_status": "success",
        "payload": json.dumps({"switchui_workflow_id": "json-workflow", "inputs": {}}),
    }
    with patch("engine.cron.poller._fetch_jobs", new=AsyncMock(return_value=[job])):
        await poller.tick()

    engine.start_run.assert_awaited_once()
    args = engine.start_run.await_args[0]
    assert args[0] == "json-workflow"
