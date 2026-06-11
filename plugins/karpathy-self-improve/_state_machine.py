"""
_state_machine.py — Experiment state machine for karpathy-self-improve.

Canonical states: proposed → approved → live → verified
                  proposed → rejected
                  approved → reverted
                  live     → reverted

All transitions are atomic (BEGIN IMMEDIATE) and write an audit row to
experiment_state_transitions.  The one-active-per-profile invariant is
enforced via the DB unique partial index; this module surfaces that as a
clear ValueError.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, FrozenSet

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed transitions
# ---------------------------------------------------------------------------

ALLOWED_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    "proposed": frozenset({"approved", "rejected"}),
    "approved": frozenset({"live", "reverted"}),
    "live":     frozenset({"verified", "reverted"}),
    "verified": frozenset(),
    "reverted": frozenset(),
    "rejected": frozenset(),
}

# Map to_state -> timestamp column to set on the experiment row.
_TIMESTAMP_COL: Dict[str, str] = {
    "approved": "approved_at",
    "live":     "applied_at",
    "verified": "verified_at",
    "reverted": "reverted_at",
    "rejected":  "rejected_at",
}

# Map to_state -> actor column (approved_by / rejected_by) if applicable.
_ACTOR_COL: Dict[str, str] = {
    "approved": "approved_by",
    "rejected": "rejected_by",
}


def transition(
    db: "KarpathyDB",  # type: ignore[name-defined]  # noqa: F821
    experiment_id: int,
    to_state: str,
    actor: str = "",
    reason: str = "",
) -> None:
    """Transition *experiment_id* to *to_state* atomically.

    Validates:
    - *to_state* is a recognised state.
    - The current state allows the transition.
    - The one-active-per-profile invariant (delegated to DB unique index).

    Writes an experiment_state_transitions audit row and updates the
    experiment row in a single BEGIN IMMEDIATE transaction.

    Raises ValueError on any violation.
    """
    from _db import VALID_STATES  # absolute import; sys.path set by loader

    if to_state not in VALID_STATES:
        raise ValueError(
            f"Unknown target state {to_state!r}. "
            f"Valid states: {sorted(VALID_STATES)}"
        )

    exp = db.get_experiment(experiment_id)
    if exp is None:
        raise ValueError(f"Experiment {experiment_id} not found.")

    from_state: str = exp["state"]
    allowed = ALLOWED_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise ValueError(
            f"Transition {from_state!r} → {to_state!r} is not allowed. "
            f"Allowed from {from_state!r}: {sorted(allowed) or '(none)'}"
        )

    now = datetime.now(timezone.utc).isoformat()

    # Build the fields to update on the experiment row.
    update_fields: Dict[str, object] = {
        "state": to_state,
        "updated_at": now,
    }
    ts_col = _TIMESTAMP_COL.get(to_state)
    if ts_col:
        update_fields[ts_col] = now
    actor_col = _ACTOR_COL.get(to_state)
    if actor_col and actor:
        update_fields[actor_col] = actor
    if to_state == "rejected" and reason:
        update_fields["rejection_reason"] = reason

    # Execute the whole update atomically.  The unique partial index on
    # experiments(profile) WHERE state IN ('proposed','approved','live') will
    # raise sqlite3.IntegrityError if another active experiment already exists
    # for this profile — we re-raise as ValueError for callers.
    import sqlite3

    try:
        # Acquire an exclusive write lock before reading + writing.
        db._conn.execute("BEGIN IMMEDIATE")
        db.update_experiment_fields(experiment_id, _commit=False, **update_fields)
        db.insert_state_transition(
            experiment_id=experiment_id,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            reason=reason,
            created_at=now,
        )
        db._conn.commit()
    except sqlite3.IntegrityError as exc:
        db._conn.rollback()
        raise ValueError(
            f"Cannot transition experiment {experiment_id} to {to_state!r}: "
            f"one-active-per-profile invariant violated (another experiment is "
            f"already active for this profile). Detail: {exc}"
        ) from exc
    except Exception:
        db._conn.rollback()
        raise

    logger.debug(
        "karpathy-self-improve: experiment %d transitioned %s → %s by %r",
        experiment_id,
        from_state,
        to_state,
        actor or "(system)",
    )
