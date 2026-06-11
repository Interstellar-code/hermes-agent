"""
test_db.py — KarpathyDB unit tests.

Uses a tmp_path-backed file DB to exercise schema creation, inserts,
queries, and constraint enforcement.  The canonical state vocabulary
(proposed/approved/live/verified/reverted/rejected) is tested here;
legacy states (pending/running/done) must be rejected.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Return a fresh KarpathyDB backed by a temp file (not :memory: so
    we exercise the path-creation logic too).  Resets the module singleton
    so each test gets an isolated connection."""
    db_file = str(tmp_path / "test-karpathy.db")
    monkeypatch.setenv("KARPATHY_DB_PATH", db_file)

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


def test_metrics_window_offsets_stored(db) -> None:
    ts = _now()
    db.insert_metrics_snapshot(
        profile="p",
        captured_at=ts,
        window_started_at=ts,
        window_ended_at=ts,
        from_offset=0,
        to_offset=4096,
    )
    rows = db.list_metrics(profile="p")
    assert rows[0]["from_offset"] == 0
    assert rows[0]["to_offset"] == 4096


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
# experiments — canonical states
# ---------------------------------------------------------------------------

def test_insert_experiment_proposed(db) -> None:
    ts = _now()
    exp_id = db.insert_experiment(
        profile="myprofile",
        file="agent/prompt.py",
        state="proposed",
        rationale="Try shorter system prompt",
        created_at=ts,
        updated_at=ts,
    )
    assert isinstance(exp_id, int)
    exp = db.get_experiment(exp_id)
    assert exp is not None
    assert exp["profile"] == "myprofile"
    assert exp["state"] == "proposed"


def test_insert_experiment_rejects_invalid_state(db) -> None:
    ts = _now()
    with pytest.raises(ValueError, match="Invalid state"):
        db.insert_experiment(
            profile="p", file="f.py", state="pending",
            created_at=ts, updated_at=ts,
        )


def test_insert_experiment_rejects_running(db) -> None:
    ts = _now()
    with pytest.raises(ValueError):
        db.insert_experiment(
            profile="p", state="running", created_at=ts, updated_at=ts,
        )


def test_insert_experiment_rejects_done(db) -> None:
    ts = _now()
    with pytest.raises(ValueError):
        db.insert_experiment(
            profile="p", state="done", created_at=ts, updated_at=ts,
        )


def test_get_experiment_missing(db) -> None:
    assert db.get_experiment(9999) is None


def test_list_experiments_filter_by_state(db) -> None:
    ts = _now()
    db.insert_experiment(profile="p", state="proposed", created_at=ts, updated_at=ts)
    # Need a different profile for the second active experiment (unique partial index).
    db.insert_experiment(profile="p2", state="approved", created_at=ts, updated_at=ts)
    db.insert_experiment(profile="p3", state="rejected", created_at=ts, updated_at=ts)

    proposed = db.list_experiments(state="proposed")
    assert len(proposed) == 1
    assert proposed[0]["state"] == "proposed"

    approved = db.list_experiments(state="approved")
    assert len(approved) == 1


def test_update_experiment_state_valid(db) -> None:
    ts = _now()
    exp_id = db.insert_experiment(
        profile="p", state="proposed", created_at=ts, updated_at=ts
    )
    db.update_experiment_state(exp_id, "rejected", verdict="reject", updated_at=_now())
    exp = db.get_experiment(exp_id)
    assert exp["state"] == "rejected"
    assert exp["verdict"] == "reject"


def test_update_experiment_state_invalid_state_raises(db) -> None:
    ts = _now()
    exp_id = db.insert_experiment(
        profile="p", state="proposed", created_at=ts, updated_at=ts
    )
    with pytest.raises(ValueError, match="Invalid state"):
        db.update_experiment_state(exp_id, "pending")


def test_one_active_per_profile_unique_index(db) -> None:
    """Inserting a second active experiment for the same profile must fail."""
    import sqlite3
    ts = _now()
    db.insert_experiment(profile="solo", state="proposed", created_at=ts, updated_at=ts)
    with pytest.raises((sqlite3.IntegrityError, ValueError)):
        db.insert_experiment(profile="solo", state="approved", created_at=ts, updated_at=ts)


def test_git_columns_stored(db) -> None:
    ts = _now()
    exp_id = db.insert_experiment(
        profile="ratchet",
        state="proposed",
        target_profile_root="/profiles/coder",
        target_relpath="agent/prompt.py",
        base_commit_sha="abc123",
        base_blob_sha="def456",
        created_at=ts,
        updated_at=ts,
    )
    exp = db.get_experiment(exp_id)
    assert exp["target_profile_root"] == "/profiles/coder"
    assert exp["base_commit_sha"] == "abc123"
    assert exp["base_blob_sha"] == "def456"


# ---------------------------------------------------------------------------
# experiment_state_transitions
# ---------------------------------------------------------------------------

def test_insert_and_list_state_transitions(db) -> None:
    ts = _now()
    exp_id = db.insert_experiment(
        profile="t", state="proposed", created_at=ts, updated_at=ts
    )
    tid = db.insert_state_transition(
        experiment_id=exp_id,
        from_state="proposed",
        to_state="approved",
        actor="human",
        reason="looks good",
        created_at=ts,
    )
    assert isinstance(tid, int)
    rows = db.list_state_transitions(exp_id)
    assert len(rows) == 1
    assert rows[0]["from_state"] == "proposed"
    assert rows[0]["to_state"] == "approved"
    assert rows[0]["actor"] == "human"


# ---------------------------------------------------------------------------
# eval_runs
# ---------------------------------------------------------------------------

def test_insert_eval_run(db) -> None:
    ts = _now()
    exp_id = db.insert_experiment(
        profile="e", state="proposed", created_at=ts, updated_at=ts
    )
    run_id = db.insert_eval_run(
        experiment_id=exp_id,
        kind="offline",
        proposer_model="claude-3-5-sonnet",
        judge_model="claude-opus-4",
        aggregate_score=0.82,
        cost=0.01,
        created_at=ts,
    )
    assert isinstance(run_id, int)


# ---------------------------------------------------------------------------
# experiment_scenario_results
# ---------------------------------------------------------------------------

def test_insert_scenario_result(db) -> None:
    ts = _now()
    exp_id = db.insert_experiment(
        profile="sr", state="proposed", created_at=ts, updated_at=ts
    )
    run_id = db.insert_eval_run(
        experiment_id=exp_id, kind="offline", created_at=ts
    )
    scen_id = db.insert_scenario(
        profile="sr", name="q1", input="hi", created_at=ts
    )
    res_id = db.insert_scenario_result(
        eval_run_id=run_id,
        scenario_id=scen_id,
        split="train",
        pass_fail=1,
        judge_rationale="Correct answer",
        scenario_snapshot={"input": "hi"},
        created_at=ts,
    )
    assert isinstance(res_id, int)


# ---------------------------------------------------------------------------
# baselines
# ---------------------------------------------------------------------------

def test_insert_and_list_baselines(db) -> None:
    ts = _now()
    bid = db.insert_baseline(
        profile="base",
        file="agent/prompt.py",
        commit_sha="aabbcc",
        score=0.75,
        created_at=ts,
    )
    assert isinstance(bid, int)
    rows = db.list_baselines("base")
    assert len(rows) == 1
    assert rows[0]["score"] == 0.75


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
