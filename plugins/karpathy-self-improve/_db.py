"""
_db.py — SQLite persistence layer for karpathy-self-improve.

DB path: get_hermes_home() / "karpathy-self-improve.db"
Override via KARPATHY_DB_PATH env var.

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

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DB_PATH = Path(
    os.environ.get("KARPATHY_DB_PATH") or str(get_hermes_home() / "karpathy-self-improve.db")
)
_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    profile        TEXT    NOT NULL,
    captured_at    TEXT    NOT NULL,
    sessions_count INTEGER NOT NULL DEFAULT 0,
    error_count    INTEGER NOT NULL DEFAULT 0,
    warn_count     INTEGER NOT NULL DEFAULT 0,
    tokens         INTEGER NOT NULL DEFAULT 0,
    cost           REAL    NOT NULL DEFAULT 0.0,
    retries        INTEGER NOT NULL DEFAULT 0,
    payload        TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS experiments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    profile       TEXT NOT NULL,
    file          TEXT NOT NULL DEFAULT '',
    state         TEXT NOT NULL DEFAULT 'pending',
    diff          TEXT NOT NULL DEFAULT '',
    rationale     TEXT NOT NULL DEFAULT '',
    offline_score REAL,
    live_score    REAL,
    verdict       TEXT,
    cost          REAL    NOT NULL DEFAULT 0.0,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
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
"""


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def _open_conn(path: Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
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
    # Re-read path each time so tests can set KARPATHY_DB_PATH before first call.
    db_path = Path(
        os.environ.get("KARPATHY_DB_PATH") or str(get_hermes_home() / "karpathy-self-improve.db")
    )
    with _lock:
        if _conn is None:
            logger.debug("karpathy-self-improve: opening DB at %s", db_path)
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
        payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        payload_json = json.dumps(payload or {})
        cur = self._conn.execute(
            """
            INSERT INTO metrics_snapshots
                (profile, captured_at, sessions_count, error_count, warn_count,
                 tokens, cost, retries, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (profile, captured_at, sessions_count, error_count, warn_count,
             tokens, cost, retries, payload_json),
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
        state: str = "pending",
        diff: str = "",
        rationale: str = "",
        offline_score: Optional[float] = None,
        live_score: Optional[float] = None,
        verdict: Optional[str] = None,
        cost: float = 0.0,
        created_at: str,
        updated_at: str,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO experiments
                (profile, file, state, diff, rationale, offline_score, live_score,
                 verdict, cost, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (profile, file, state, diff, rationale, offline_score, live_score,
             verdict, cost, created_at, updated_at),
        )
        self._conn.commit()
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

    def update_experiment_state(
        self,
        exp_id: int,
        state: str,
        **fields: Any,
    ) -> None:
        """Update state and any additional fields (offline_score, verdict, etc.)."""
        allowed = {
            "diff", "rationale", "offline_score", "live_score",
            "verdict", "cost", "updated_at",
        }
        extra = {k: v for k, v in fields.items() if k in allowed}
        set_parts = ["state = ?"]
        params: List[Any] = [state]
        for col, val in extra.items():
            set_parts.append(f"{col} = ?")
            params.append(val)
        params.append(exp_id)
        self._conn.execute(
            f"UPDATE experiments SET {', '.join(set_parts)} WHERE id = ?",
            params,
        )
        self._conn.commit()

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
