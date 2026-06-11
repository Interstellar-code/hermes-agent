"""
test_state_machine.py — State machine unit tests.

Covers:
  - Legal transitions write audit row and update experiment
  - Illegal transitions raise ValueError
  - Terminal states (verified/reverted/rejected) have no exits
  - One-active-per-profile invariant raises when a second active experiment
    would be created in the same profile via a transition
  - Timestamp columns are set on transition
  - actor/reason fields propagated
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "sm-test.db")
    monkeypatch.setenv("KARPATHY_DB_PATH", db_file)
    import _db as db_mod
    monkeypatch.setattr(db_mod, "_conn", None)
    from _db import open_db
    return open_db(Path(db_file))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_exp(db, profile: str = "p", state: str = "proposed") -> int:
    ts = _now()
    return db.insert_experiment(
        profile=profile, state=state, created_at=ts, updated_at=ts
    )


# ---------------------------------------------------------------------------
# Legal transitions
# ---------------------------------------------------------------------------

def test_proposed_to_approved(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    transition(db, exp_id, "approved", actor="human", reason="LGTM")
    exp = db.get_experiment(exp_id)
    assert exp["state"] == "approved"
    assert exp["approved_by"] == "human"
    assert exp["approved_at"] is not None


def test_proposed_to_rejected(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    transition(db, exp_id, "rejected", actor="judge", reason="score too low")
    exp = db.get_experiment(exp_id)
    assert exp["state"] == "rejected"
    assert exp["rejected_by"] == "judge"
    assert exp["rejection_reason"] == "score too low"
    assert exp["rejected_at"] is not None


def test_approved_to_live(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    transition(db, exp_id, "approved")
    transition(db, exp_id, "live")
    exp = db.get_experiment(exp_id)
    assert exp["state"] == "live"
    assert exp["applied_at"] is not None


def test_live_to_verified(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    transition(db, exp_id, "approved")
    transition(db, exp_id, "live")
    transition(db, exp_id, "verified")
    exp = db.get_experiment(exp_id)
    assert exp["state"] == "verified"
    assert exp["verified_at"] is not None


def test_live_to_reverted(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    transition(db, exp_id, "approved")
    transition(db, exp_id, "live")
    transition(db, exp_id, "reverted")
    exp = db.get_experiment(exp_id)
    assert exp["state"] == "reverted"
    assert exp["reverted_at"] is not None


def test_approved_to_reverted(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    transition(db, exp_id, "approved")
    transition(db, exp_id, "reverted")
    exp = db.get_experiment(exp_id)
    assert exp["state"] == "reverted"


# ---------------------------------------------------------------------------
# Audit rows written
# ---------------------------------------------------------------------------

def test_audit_row_written_on_transition(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    transition(db, exp_id, "approved", actor="tester", reason="all good")
    rows = db.list_state_transitions(exp_id)
    assert len(rows) == 1
    assert rows[0]["from_state"] == "proposed"
    assert rows[0]["to_state"] == "approved"
    assert rows[0]["actor"] == "tester"
    assert rows[0]["reason"] == "all good"


def test_multiple_audit_rows(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    transition(db, exp_id, "approved")
    transition(db, exp_id, "live")
    transition(db, exp_id, "verified")
    rows = db.list_state_transitions(exp_id)
    assert len(rows) == 3
    states = [(r["from_state"], r["to_state"]) for r in rows]
    assert ("proposed", "approved") in states
    assert ("approved", "live") in states
    assert ("live", "verified") in states


# ---------------------------------------------------------------------------
# Illegal transitions
# ---------------------------------------------------------------------------

def test_proposed_to_live_illegal(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    with pytest.raises(ValueError, match="not allowed"):
        transition(db, exp_id, "live")


def test_proposed_to_verified_illegal(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    with pytest.raises(ValueError, match="not allowed"):
        transition(db, exp_id, "verified")


def test_proposed_to_reverted_illegal(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    with pytest.raises(ValueError, match="not allowed"):
        transition(db, exp_id, "reverted")


def test_verified_is_terminal(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    transition(db, exp_id, "approved")
    transition(db, exp_id, "live")
    transition(db, exp_id, "verified")
    with pytest.raises(ValueError, match="not allowed"):
        transition(db, exp_id, "reverted")


def test_rejected_is_terminal(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    transition(db, exp_id, "rejected")
    with pytest.raises(ValueError, match="not allowed"):
        transition(db, exp_id, "approved")


def test_reverted_is_terminal(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    transition(db, exp_id, "approved")
    transition(db, exp_id, "reverted")
    with pytest.raises(ValueError, match="not allowed"):
        transition(db, exp_id, "live")


def test_invalid_target_state_raises(db) -> None:
    from _state_machine import transition
    exp_id = _make_exp(db, state="proposed")
    with pytest.raises(ValueError, match="Unknown target state"):
        transition(db, exp_id, "running")


def test_missing_experiment_raises(db) -> None:
    from _state_machine import transition
    with pytest.raises(ValueError, match="not found"):
        transition(db, 99999, "approved")


# ---------------------------------------------------------------------------
# One-active-per-profile invariant
# ---------------------------------------------------------------------------

def test_one_active_per_profile_via_transition(db) -> None:
    """After exp1 is rejected (terminal), exp2 can be approved for same profile."""
    from _state_machine import transition
    # exp1: proposed → rejected (leaves no active row for "same")
    exp1 = _make_exp(db, profile="same", state="proposed")
    transition(db, exp1, "rejected")

    # exp2 for the same profile can now be proposed and approved.
    exp2 = _make_exp(db, profile="same", state="proposed")
    transition(db, exp2, "approved")
    assert db.get_experiment(exp2)["state"] == "approved"


def test_two_active_experiments_same_profile_fails(db) -> None:
    """Two concurrent active experiments for the same profile must not coexist."""
    import sqlite3
    from _state_machine import transition

    exp1 = _make_exp(db, profile="conflict", state="proposed")
    # exp1 is active (proposed). Inserting another active exp for same profile
    # must raise — the unique partial index fires.
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        _make_exp(db, profile="conflict", state="approved")
