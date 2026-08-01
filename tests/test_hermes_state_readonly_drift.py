"""Read-only aggregation over a profile DB whose schema predates the query.

Cross-profile aggregation opens other profiles' ``state.db`` read-only. A
profile whose gateway last ran on an older version keeps that version's
``sessions`` table, so every aggregation query selecting a newer column used to
fail for the whole profile ("no such column: s.display_name") — real sessions
rendered as zero rows plus an error string.
"""
import sqlite3

import pytest

import hermes_state
from hermes_state import SessionDB

# Columns the current SCHEMA_SQL declares that older profile DBs lack. Mirrors
# the live drift: neo/morpheus were missing the first three, trinity all four.
DRIFTED_COLUMNS = ("display_name", "archived", "origin_json", "expiry_finalized")


def _make_db(tmp_path, drop=DRIFTED_COLUMNS):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    session_id = db.create_session("old-session", "api_server")
    db.close()
    if drop:
        conn = sqlite3.connect(str(path))
        for col in drop:
            conn.execute(f'ALTER TABLE sessions DROP COLUMN "{col}"')
        conn.commit()
        conn.close()
    return path, session_id


@pytest.fixture(autouse=True)
def _fresh_drift_guard(monkeypatch):
    """The reconcile is one-shot per path per process; isolate per test."""
    monkeypatch.setattr(hermes_state, "_drift_checked_paths", set())


def _live_columns(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {row[1] for row in conn.execute('PRAGMA table_info("sessions")')}
    finally:
        conn.close()


def test_drifted_db_fails_before_reconcile(tmp_path):
    """Anchor: this is exactly the live dashboard error being fixed."""
    path, _ = _make_db(tmp_path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            conn.execute("SELECT s.display_name FROM sessions s").fetchall()
    finally:
        conn.close()


def test_readonly_open_reconciles_drifted_columns(tmp_path):
    path, session_id = _make_db(tmp_path)
    assert not (set(DRIFTED_COLUMNS) & _live_columns(path))

    db = SessionDB(db_path=path, read_only=True)
    try:
        rows = db.list_sessions_rich(limit=10)
    finally:
        db.close()

    # The whole point: the profile's sessions surface instead of an error.
    assert [r["id"] for r in rows] == [session_id]
    assert set(DRIFTED_COLUMNS) <= _live_columns(path)


def test_current_db_is_never_opened_writable(tmp_path):
    """A non-drifted DB must keep the read-only guarantee intact — no write
    lock, not even briefly. Enforced by making the file itself unwritable."""
    path, session_id = _make_db(tmp_path, drop=())
    path.chmod(0o444)
    try:
        db = SessionDB(db_path=path, read_only=True)
        try:
            rows = db.list_sessions_rich(limit=10)
        finally:
            db.close()
    finally:
        path.chmod(0o644)

    assert [r["id"] for r in rows] == [session_id]


def test_unwritable_drifted_db_degrades_instead_of_raising(tmp_path):
    """If the reconcile can't take the write lock we give up quietly and the
    caller degrades exactly as it does today — no exception out of __init__."""
    path, _ = _make_db(tmp_path)
    path.chmod(0o444)
    try:
        db = SessionDB(db_path=path, read_only=True)
        db.close()
    finally:
        path.chmod(0o644)

    assert not (set(DRIFTED_COLUMNS) & _live_columns(path))


def test_reconcile_is_attempted_once_per_path(tmp_path):
    """A drifted DB we couldn't fix must not be re-probed on every poll."""
    path, _ = _make_db(tmp_path)
    SessionDB(db_path=path, read_only=True).close()
    assert str(path) in hermes_state._drift_checked_paths
