"""
_db.py — SQLite persistence layer for karpathy-self-improve.

DB path resolution (highest to lowest priority):
  1. KARPATHY_DB_PATH env var
  2. config.yaml key plugins.karpathy_self_improve.db_path
  3. get_default_hermes_root() / "karpathy-self-improve.db"

WAL mode, row_factory=sqlite3.Row, threading lock singleton.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_default_hermes_root

logger = logging.getLogger(__name__)

# Re-evaluated on every get_db() call so tests can set KARPATHY_DB_PATH before
# the first access without module-load ordering issues.
_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Canonical state vocabulary
# ---------------------------------------------------------------------------

VALID_STATES = frozenset({"proposed", "approved", "live", "verified", "reverted", "rejected"})

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    profile            TEXT    NOT NULL,
    captured_at        TEXT    NOT NULL,
    sessions_count     INTEGER NOT NULL DEFAULT 0,
    error_count        INTEGER NOT NULL DEFAULT 0,
    warn_count         INTEGER NOT NULL DEFAULT 0,
    tokens             INTEGER NOT NULL DEFAULT 0,
    cost               REAL    NOT NULL DEFAULT 0.0,
    retries            INTEGER NOT NULL DEFAULT 0,
    window_started_at  TEXT,
    window_ended_at    TEXT,
    from_offset        INTEGER,
    to_offset          INTEGER,
    payload            TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS experiments (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    profile                     TEXT    NOT NULL,
    file                        TEXT    NOT NULL DEFAULT '',
    state                       TEXT    NOT NULL DEFAULT 'proposed'
                                    CHECK(state IN ('proposed','approved','live',
                                                    'verified','reverted','rejected')),
    diff                        TEXT    NOT NULL DEFAULT '',
    rationale                   TEXT    NOT NULL DEFAULT '',
    offline_score               REAL,
    live_score                  REAL,
    verdict                     TEXT,
    cost                        REAL    NOT NULL DEFAULT 0.0,
    -- git ratchet columns
    target_profile_root         TEXT,
    target_relpath              TEXT,
    base_commit_sha             TEXT,
    apply_commit_sha            TEXT,
    revert_commit_sha           TEXT,
    base_blob_sha               TEXT,
    dirty_before_apply          INTEGER NOT NULL DEFAULT 0,
    manual_conflict_detected    INTEGER NOT NULL DEFAULT 0,
    -- lifecycle / approval columns
    approved_by                 TEXT,
    approved_at                 TEXT,
    rejected_by                 TEXT,
    rejected_at                 TEXT,
    rejection_reason            TEXT,
    live_sessions_target        INTEGER,
    live_sessions_observed      INTEGER NOT NULL DEFAULT 0,
    applied_at                  TEXT,
    verified_at                 TEXT,
    reverted_at                 TEXT,
    -- reference
    baseline_id                 INTEGER,
    proposer_model              TEXT,
    judge_model                 TEXT,
    sentence_delta_count        INTEGER,
    created_at                  TEXT    NOT NULL,
    updated_at                  TEXT    NOT NULL
);

-- At most one experiment per profile in an active state.
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_experiment_per_profile
    ON experiments(profile)
    WHERE state IN ('proposed', 'approved', 'live');

CREATE TABLE IF NOT EXISTS experiment_state_transitions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL,
    from_state    TEXT    NOT NULL,
    to_state      TEXT    NOT NULL,
    actor         TEXT    NOT NULL DEFAULT '',
    reason        TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id   INTEGER NOT NULL,
    kind            TEXT    NOT NULL CHECK(kind IN ('offline', 'live')),
    proposer_model  TEXT,
    judge_model     TEXT,
    aggregate_score REAL,
    cost            REAL,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_scenario_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_run_id       INTEGER NOT NULL,
    scenario_id       INTEGER NOT NULL,
    split             TEXT    NOT NULL CHECK(split IN ('train', 'holdout')),
    pass_fail         INTEGER NOT NULL DEFAULT 0,
    judge_rationale   TEXT    NOT NULL DEFAULT '',
    scenario_snapshot TEXT    NOT NULL DEFAULT '{}',
    created_at        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS baselines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    profile       TEXT    NOT NULL,
    file          TEXT    NOT NULL DEFAULT '',
    commit_sha    TEXT,
    score         REAL,
    experiment_id INTEGER,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS scenarios (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    profile    TEXT    NOT NULL,
    name       TEXT    NOT NULL,
    input      TEXT    NOT NULL DEFAULT '',
    checks     TEXT    NOT NULL DEFAULT '[]',
    holdout    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS controls (
    profile TEXT    PRIMARY KEY,
    paused  INTEGER NOT NULL DEFAULT 0
);
"""


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def resolve_db_path() -> Path:
    """Return the resolved DB path using 3-level precedence.

    1. KARPATHY_DB_PATH env var (highest — tests rely on it).
    2. config.yaml key plugins.karpathy_self_improve.db_path.
       Expands ~ and env vars; relative paths resolved under hermes root.
    3. Default: get_default_hermes_root() / "karpathy-self-improve.db".

    Never raises — falls back to default on any config read error.
    """
    # Level 1: env var.
    env = os.environ.get("KARPATHY_DB_PATH")
    if env:
        return Path(env)

    # Level 2: config.yaml.
    try:
        from hermes_cli.config import load_config, cfg_get  # type: ignore[import]
        config = load_config()
        cfg_val = cfg_get(config, "plugins", "karpathy_self_improve", "db_path", default=None)
        if cfg_val is not None:
            expanded = os.path.expandvars(os.path.expanduser(str(cfg_val)))
            p = Path(expanded)
            if not p.is_absolute():
                p = get_default_hermes_root() / p
            return p
    except Exception:
        pass  # Config unavailable (bare tests, etc.) — fall through to default.

    # Level 3: default.
    return get_default_hermes_root() / "karpathy-self-improve.db"


# Keep _get_db_path as a thin alias so any external callers are not broken.
def _get_db_path() -> Path:
    return resolve_db_path()


def _open_conn(path: Path) -> sqlite3.Connection:
    is_memory = str(path) == ":memory:"
    if not is_memory:
        exists_before = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not exists_before:
            logger.info(
                "karpathy-self-improve: initializing metrics DB at %s", path
            )
        else:
            logger.debug("karpathy-self-improve: opening DB at %s", path)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply CREATE IF NOT EXISTS migrations. Safe on fresh or existing DB."""
    conn.executescript(_SCHEMA_SQL)
    conn.commit()


def get_db() -> "KarpathyDB":
    """Return the process-wide KarpathyDB singleton, opening on first call."""
    global _conn
    db_path = resolve_db_path()
    with _lock:
        if _conn is None:
            _conn = _open_conn(db_path)
    return KarpathyDB(_conn)


def open_db(path: Path) -> "KarpathyDB":
    """Open a fresh connection to *path* (used by tests / CLI for isolated DBs)."""
    conn = _open_conn(path)
    return KarpathyDB(conn)


# ---------------------------------------------------------------------------
# KarpathyDB
# ---------------------------------------------------------------------------

class KarpathyDB:
    """Thin wrapper around a sqlite3 connection. All methods use parametrised SQL."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # --- metrics_snapshots --------------------------------------------------

    def insert_metrics_snapshot(
        self,
        *,
        profile: str,
        captured_at: str,
        sessions_count: int = 0,
        error_count: int = 0,
        warn_count: int = 0,
        tokens: int = 0,
        cost: float = 0.0,
        retries: int = 0,
        window_started_at: Optional[str] = None,
        window_ended_at: Optional[str] = None,
        from_offset: Optional[int] = None,
        to_offset: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        payload_json = json.dumps(payload or {})
        cur = self._conn.execute(
            """
            INSERT INTO metrics_snapshots
                (profile, captured_at, sessions_count, error_count, warn_count,
                 tokens, cost, retries, window_started_at, window_ended_at,
                 from_offset, to_offset, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (profile, captured_at, sessions_count, error_count, warn_count,
             tokens, cost, retries, window_started_at, window_ended_at,
             from_offset, to_offset, payload_json),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def list_metrics(
        self,
        profile: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        if profile:
            cur = self._conn.execute(
                "SELECT * FROM metrics_snapshots WHERE profile = ? ORDER BY captured_at DESC LIMIT ?",
                (profile, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM metrics_snapshots ORDER BY captured_at DESC LIMIT ?",
                (limit,),
            )
        return [dict(row) for row in cur.fetchall()]

    def latest_metrics_per_profile(self) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT * FROM metrics_snapshots
            WHERE id IN (
                SELECT MAX(id) FROM metrics_snapshots GROUP BY profile
            )
            ORDER BY profile
            """
        )
        return [dict(row) for row in cur.fetchall()]

    # --- experiments --------------------------------------------------------

    def insert_experiment(
        self,
        *,
        profile: str,
        file: str = "",
        state: str = "proposed",
        diff: str = "",
        rationale: str = "",
        offline_score: Optional[float] = None,
        live_score: Optional[float] = None,
        verdict: Optional[str] = None,
        cost: float = 0.0,
        target_profile_root: Optional[str] = None,
        target_relpath: Optional[str] = None,
        base_commit_sha: Optional[str] = None,
        base_blob_sha: Optional[str] = None,
        live_sessions_target: Optional[int] = None,
        baseline_id: Optional[int] = None,
        proposer_model: Optional[str] = None,
        judge_model: Optional[str] = None,
        sentence_delta_count: Optional[int] = None,
        created_at: str,
        updated_at: str,
    ) -> int:
        if state not in VALID_STATES:
            raise ValueError(f"Invalid state {state!r}. Must be one of {sorted(VALID_STATES)}")
        # The unique partial index enforces one-active-per-profile. Use BEGIN
        # IMMEDIATE so the read-check-insert is atomic and gives a clear error.
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                """
                INSERT INTO experiments
                    (profile, file, state, diff, rationale, offline_score, live_score,
                     verdict, cost, target_profile_root, target_relpath,
                     base_commit_sha, base_blob_sha, live_sessions_target,
                     baseline_id, proposer_model, judge_model, sentence_delta_count,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (profile, file, state, diff, rationale, offline_score, live_score,
                 verdict, cost, target_profile_root, target_relpath,
                 base_commit_sha, base_blob_sha, live_sessions_target,
                 baseline_id, proposer_model, judge_model, sentence_delta_count,
                 created_at, updated_at),
            )
        return cur.lastrowid  # type: ignore[return-value]

    def get_experiment(self, exp_id: int) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM experiments WHERE id = ?", (exp_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_experiments(
        self,
        profile: Optional[str] = None,
        state: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if profile:
            clauses.append("profile = ?")
            params.append(profile)
        if state:
            clauses.append("state = ?")
            params.append(state)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = self._conn.execute(
            f"SELECT * FROM experiments {where} ORDER BY created_at DESC",
            params,
        )
        return [dict(row) for row in cur.fetchall()]

    def update_experiment_fields(
        self,
        exp_id: int,
        _commit: bool = True,
        **fields: Any,
    ) -> None:
        """Update arbitrary allowed columns. Does NOT enforce state-machine rules;
        use _state_machine.transition() for state changes.

        Pass _commit=False when calling from inside an existing transaction
        (e.g. _state_machine.transition()) so the caller owns the single commit
        and all writes remain atomic under BEGIN IMMEDIATE.
        """
        allowed = {
            "state", "diff", "rationale", "offline_score", "live_score",
            "verdict", "cost", "target_profile_root", "target_relpath",
            "base_commit_sha", "apply_commit_sha", "revert_commit_sha",
            "base_blob_sha", "dirty_before_apply", "manual_conflict_detected",
            "approved_by", "approved_at", "rejected_by", "rejected_at",
            "rejection_reason", "live_sessions_target", "live_sessions_observed",
            "applied_at", "verified_at", "reverted_at", "baseline_id",
            "proposer_model", "judge_model", "sentence_delta_count", "updated_at",
        }
        extra = {k: v for k, v in fields.items() if k in allowed}
        if not extra:
            return
        set_parts = [f"{col} = ?" for col in extra]
        params: List[Any] = list(extra.values())
        params.append(exp_id)
        self._conn.execute(
            f"UPDATE experiments SET {', '.join(set_parts)} WHERE id = ?",
            params,
        )
        if _commit:
            self._conn.commit()

    # Legacy alias — kept so existing callers that only change state still work.
    def update_experiment_state(
        self,
        exp_id: int,
        state: str,
        **fields: Any,
    ) -> None:
        """Update state and any additional fields. Validates state vocabulary."""
        if state not in VALID_STATES:
            raise ValueError(f"Invalid state {state!r}. Must be one of {sorted(VALID_STATES)}")
        self.update_experiment_fields(exp_id, state=state, **fields)

    # --- experiment_state_transitions ---------------------------------------

    def insert_state_transition(
        self,
        *,
        experiment_id: int,
        from_state: str,
        to_state: str,
        actor: str = "",
        reason: str = "",
        created_at: str,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO experiment_state_transitions
                (experiment_id, from_state, to_state, actor, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (experiment_id, from_state, to_state, actor, reason, created_at),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def list_state_transitions(self, experiment_id: int) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM experiment_state_transitions WHERE experiment_id = ? ORDER BY id",
            (experiment_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    # --- eval_runs ----------------------------------------------------------

    def insert_eval_run(
        self,
        *,
        experiment_id: int,
        kind: str,
        proposer_model: Optional[str] = None,
        judge_model: Optional[str] = None,
        aggregate_score: Optional[float] = None,
        cost: Optional[float] = None,
        created_at: str,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO eval_runs
                (experiment_id, kind, proposer_model, judge_model, aggregate_score,
                 cost, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (experiment_id, kind, proposer_model, judge_model, aggregate_score,
             cost, created_at),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    # --- experiment_scenario_results ----------------------------------------

    def insert_scenario_result(
        self,
        *,
        eval_run_id: int,
        scenario_id: int,
        split: str,
        pass_fail: int = 0,
        judge_rationale: str = "",
        scenario_snapshot: Optional[Dict[str, Any]] = None,
        created_at: str,
    ) -> int:
        snap_json = json.dumps(scenario_snapshot or {})
        cur = self._conn.execute(
            """
            INSERT INTO experiment_scenario_results
                (eval_run_id, scenario_id, split, pass_fail, judge_rationale,
                 scenario_snapshot, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (eval_run_id, scenario_id, split, pass_fail, judge_rationale,
             snap_json, created_at),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    # --- baselines ----------------------------------------------------------

    def insert_baseline(
        self,
        *,
        profile: str,
        file: str = "",
        commit_sha: Optional[str] = None,
        score: Optional[float] = None,
        experiment_id: Optional[int] = None,
        created_at: str,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO baselines (profile, file, commit_sha, score, experiment_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (profile, file, commit_sha, score, experiment_id, created_at),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def list_baselines(self, profile: str) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM baselines WHERE profile = ? ORDER BY created_at DESC",
            (profile,),
        )
        return [dict(row) for row in cur.fetchall()]

    # --- scenarios ----------------------------------------------------------

    def list_scenarios(self, profile: str) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM scenarios WHERE profile = ? ORDER BY created_at DESC",
            (profile,),
        )
        return [dict(row) for row in cur.fetchall()]

    def insert_scenario(
        self,
        *,
        profile: str,
        name: str,
        input: str = "",
        checks: Optional[List[Any]] = None,
        holdout: int = 0,
        created_at: str,
    ) -> int:
        checks_json = json.dumps(checks or [])
        cur = self._conn.execute(
            """
            INSERT INTO scenarios (profile, name, input, checks, holdout, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (profile, name, input, checks_json, holdout, created_at),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_scenario(self, scenario_id: int) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM scenarios WHERE id = ?", (scenario_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def delete_scenario(self, scenario_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM scenarios WHERE id = ?", (scenario_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    # --- controls (pause/resume) ---------------------------------------------

    def set_paused(self, profile: str, paused: bool) -> None:
        self._conn.execute(
            """
            INSERT INTO controls (profile, paused) VALUES (?, ?)
            ON CONFLICT(profile) DO UPDATE SET paused = excluded.paused
            """,
            (profile, 1 if paused else 0),
        )
        self._conn.commit()

    def is_paused(self, profile: str) -> bool:
        cur = self._conn.execute(
            "SELECT paused FROM controls WHERE profile = ?", (profile,)
        )
        row = cur.fetchone()
        return bool(row and row[0])

    # --- eval_runs query -----------------------------------------------------

    def list_eval_runs(self, experiment_id: int) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM eval_runs WHERE experiment_id = ? ORDER BY id",
            (experiment_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    def list_scenario_results(self, eval_run_id: int) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM experiment_scenario_results WHERE eval_run_id = ? ORDER BY id",
            (eval_run_id,),
        )
        return [dict(row) for row in cur.fetchall()]
