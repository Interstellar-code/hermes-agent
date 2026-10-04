"""Decision log for system-one-router — SQLite, WAL, hash-only inputs.

Schema note: input_hash stores sha256(state JSON) — NEVER raw message text
(no second copy of chat content on disk). There is NO latency instrumentation
in this module: latency_ms is a plain integer the caller supplies and this
module merely stores it.

Month boundary (finding 8, by design, documented not changed): the 'month'
column and month_spend_usd() derive from SQLite ``strftime('%Y-%m','now')`` /
``datetime('now')``, i.e. the month is a UTC month, not a local-time month.
Both the row's ts and its month come from the same single SQLite evaluation
of 'now', so a row can never straddle a boundary (ts and month always agree).
Callers near a UTC month end may see the rollover a few hours off their local
calendar; that is accepted behavior, not a bug.

Operational notes:
- The schema is ensured once per (process, resolved db path): running
  executescript on every call dominated the cost of a two-row session.
- Connections are opened per call and closed via contextlib.closing; sqlite3's
  ``with conn:`` only manages the transaction, it never closes the connection.
- Failures in record()/month_spend_usd()/stats() log a warning (this module's
  logger) and return a safe default instead of vanishing silently.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    input_hash TEXT NOT NULL,
    question_kind TEXT NOT NULL DEFAULT '',
    choice TEXT,
    confidence REAL,
    status TEXT NOT NULL,
    reason TEXT,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    month TEXT NOT NULL DEFAULT (strftime('%Y-%m', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_decisions_month ON decisions(month);
"""

RETENTION_DAYS = 90

# Resolved db paths whose schema has been ensured in this process.
_schema_ready: set[str] = set()
# Serializes the one-time WAL+schema ensure per process: without it, the first
# concurrent wave all try the (briefly exclusive) WAL pragma and schema DDL at
# once and lose some races with 'database is locked'.
_ensure_lock = threading.Lock()


def hash_state(state: Any) -> str:
    if not isinstance(state, str):
        state = json.dumps(state, sort_keys=True, default=str)
    return hashlib.sha256(state.encode()).hexdigest()


class DecisionLog:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or self._default_path()

    @staticmethod
    def _default_path() -> Path:
        try:
            from hermes_constants import get_hermes_home

            return Path(get_hermes_home()) / "system-one-router.db"
        except Exception:
            return Path.home() / ".hermes" / "system-one-router.db"

    def _connect(self) -> sqlite3.Connection:
        # connect(timeout=5.0) is the only busy-handling knob; the separate
        # busy_timeout PRAGMA added nothing but a second, conflicting value.
        # WAL is a persistent database property and is set once (inside the
        # locked _ensure_schema), not per open: setting it on every open made
        # concurrent openers race for the brief exclusive lock it requires and
        # drop records with 'database is locked'.
        return sqlite3.connect(self.db_path, timeout=5.0)

    def _resolved_key(self) -> str:
        """Stable per-process key for this db path (schema/WAL dedup)."""
        try:
            return str(Path(self.db_path).resolve())
        except Exception:
            return str(self.db_path)

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        key = self._resolved_key()
        if key in _schema_ready:
            return
        with _ensure_lock:
            if key in _schema_ready:  # double-checked: a racing ensure may win
                return
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            # Retention: prune once per process, not per call.
            conn.execute("DELETE FROM decisions WHERE ts < datetime('now', ?)",
                         (f"-{RETENTION_DAYS} days",))
            conn.commit()
            _schema_ready.add(key)

    def record(
        self,
        input_hash: str,
        question_kind: str,
        choice: str | None,
        confidence: float | None,
        status: str,
        reason: str | None,
        latency_ms: int,
        cost_usd: float,
    ) -> None:
        """Insert one decision row. Never raises (log failure must not break the tool),
        but a failure is logged as a warning — silent loss used to hide spend from the cap."""
        try:
            with closing(self._connect()) as conn:
                self._ensure_schema(conn)
                with conn:
                    conn.execute(
                        "INSERT INTO decisions (input_hash, question_kind, choice, confidence,"
                        " status, reason, latency_ms, cost_usd, month)"
                        " VALUES (?,?,?,?,?,?,?,?, strftime('%Y-%m','now'))",
                        (input_hash, question_kind, choice, confidence, status, reason,
                         int(latency_ms), float(cost_usd)),
                    )
        except Exception as exc:
            log.warning("decision log record failed: %s", exc)

    def month_spend_usd(self) -> float:
        try:
            with closing(self._connect()) as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0) FROM decisions"
                    " WHERE month = strftime('%Y-%m','now')"
                ).fetchone()
                return float(row[0] or 0.0)
        except Exception as exc:
            log.warning("decision log month_spend read failed: %s", exc)
            return 0.0

    def stats(self) -> dict:
        try:
            with closing(self._connect()) as conn:
                self._ensure_schema(conn)
                total = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
                month = conn.execute(
                    "SELECT COUNT(*) FROM decisions WHERE month = strftime('%Y-%m','now')"
                ).fetchone()[0]
                # p95 via SQL OFFSET: no latency rows loaded into memory.
                row = conn.execute(
                    "SELECT latency_ms FROM decisions WHERE month = strftime('%Y-%m','now')"
                    " ORDER BY latency_ms LIMIT 1 OFFSET ?",
                    (min(int(month * 0.95), max(month - 1, 0)),)).fetchone()
                p95_val = row[0] if row else 0
                return {
                    "total_decisions": total,
                    "month_decisions": month,
                    "month_spend_usd": round(self.month_spend_usd(), 4),
                    "p95_latency_ms": p95_val,
                }
        except Exception as exc:
            log.warning("decision log stats read failed: %s", exc)
            return {"total_decisions": 0, "month_decisions": 0,
                    "month_spend_usd": 0.0, "p95_latency_ms": 0}
