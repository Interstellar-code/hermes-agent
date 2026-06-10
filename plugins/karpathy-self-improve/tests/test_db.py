"""
test_db.py — KarpathyDB unit tests.

Uses KARPATHY_DB_PATH=:memory: via monkeypatch / env override to avoid
touching the real ~/.hermes/karpathy-self-improve.db.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Return a fresh KarpathyDB backed by a temp file (not :memory: so
    we can test the path-creation logic too)."""
    db_file = str(tmp_path / "test-karpathy.db")
    monkeypatch.setenv("KARPATHY_DB_PATH", db_file)

    # Reset the singleton so get_db() opens a fresh connection for each test.
    import _db as db_mod
    monkeypatch.setattr(db_mod, "_conn", None)

    from _db import open_db
    return open_db(Path(db_file))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# metrics_snapshots
# ---------------------------------------------------------------------------

def test_insert_and_list_metrics(db) -> None:
    ts = _now()
    row_id = db.insert_metrics_snapshot(
        profile="test-profile",
        captured_at=ts,
        sessions_count=5,
        error_count=2,
        warn_count=10,
        tokens=1234,
        cost=0.05,
        retries=1,
    )
    assert isinstance(row_id, int)
    assert row_id >= 1

    rows = db.list_metrics(profile="test-profile")
    assert len(rows) == 1
    assert rows[0]["profile"] == "test-profile"
    assert rows[0]["sessions_count"] == 5
    assert rows[0]["error_count"] == 2
    assert rows[0]["tokens"] == 1234


def test_list_metrics_no_filter(db) -> None:
    ts = _now()
    db.insert_metrics_snapshot(profile="a", captured_at=ts)
    db.insert_metrics_snapshot(profile="b", captured_at=ts)
    rows = db.list_metrics()
    assert len(rows) == 2


def test_latest_metrics_per_profile(db) -> None:
    ts1 = "2026-01-01T00:00:00+00:00"
    ts2 = "2026-06-01T00:00:00+00:00"
    db.insert_metrics_snapshot(profile="x", captured_at=ts1, sessions_count=1)
    db.insert_metrics_snapshot(profile="x", captured_at=ts2, sessions_count=9)
    db.insert_metrics_snapshot(profile="y", captured_at=ts1, sessions_count=3)

    latest = db.latest_metrics_per_profile()
    by_profile = {r["profile"]: r for r in latest}
    assert by_profile["x"]["sessions_count"] == 9
    assert by_profile["y"]["sessions_count"] == 3


# ---------------------------------------------------------------------------
# experiments
# ---------------------------------------------------------------------------

def test_insert_and_get_experiment(db) -> None:
    ts = _now()
    exp_id = db.insert_experiment(
        profile="myprofile",
        file="agent/prompt.py",
        state="pending",
        rationale="Try shorter system prompt",
        created_at=ts,
        updated_at=ts,
    )
    assert isinstance(exp_id, int)

    exp = db.get_experiment(exp_id)
    assert exp is not None
    assert exp["profile"] == "myprofile"
    assert exp["state"] == "pending"
    assert exp["file"] == "agent/prompt.py"


def test_get_experiment_missing(db) -> None:
    assert db.get_experiment(9999) is None


def test_list_experiments_filter(db) -> None:
    ts = _now()
    db.insert_experiment(profile="p", state="pending", created_at=ts, updated_at=ts)
    db.insert_experiment(profile="p", state="running", created_at=ts, updated_at=ts)
    db.insert_experiment(profile="p", state="done", created_at=ts, updated_at=ts)

    pending = db.list_experiments(profile="p", state="pending")
    assert len(pending) == 1
    assert pending[0]["state"] == "pending"


def test_update_experiment_state(db) -> None:
    ts = _now()
    exp_id = db.insert_experiment(
        profile="p", state="pending", created_at=ts, updated_at=ts
    )
    db.update_experiment_state(exp_id, "done", verdict="accept", updated_at=_now())

    exp = db.get_experiment(exp_id)
    assert exp["state"] == "done"
    assert exp["verdict"] == "accept"


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------

def test_insert_and_list_scenarios(db) -> None:
    ts = _now()
    sid = db.insert_scenario(
        profile="prof",
        name="basic-qa",
        input="What is 2+2?",
        checks=["contains:4"],
        created_at=ts,
    )
    assert isinstance(sid, int)

    rows = db.list_scenarios("prof")
    assert len(rows) == 1
    assert rows[0]["name"] == "basic-qa"


def test_list_scenarios_empty(db) -> None:
    rows = db.list_scenarios("no-such-profile")
    assert rows == []
