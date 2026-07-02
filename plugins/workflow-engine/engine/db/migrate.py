"""
DB migration runner for the workflow engine.

Mirrors the TS migrate.ts logic exactly:
- Migrations live in engine/db/migrations/*.sql, named NNN_*.sql
- schema_meta.schema_version tracks the current version
- Each migration runs in a transaction; schema_version is upserted after

Cross-process safety: ensure_schema() acquires an OS-level file lock
(HERMES_HOME/switchui-workflows.db.migrate.lock) and a SQLite BEGIN EXCLUSIVE
around the schema check+apply. This prevents races between the dashboard
process and the workflow daemon both calling ensure_schema() on first boot
or after an upgrade.

Usage:
    from engine.db.client import open_db
    from engine.db.migrate import ensure_schema

    with open_db("/path/to/workflow.db") as conn:
        ensure_schema(conn)
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

from hermes_constants import get_hermes_home
from typing import Optional

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_DEFAULT_LOCK_PATH = get_hermes_home() / "switchui-workflows.db.migrate.lock"


def _migration_version(filename: str) -> int:
    match = re.match(r"^(\d+)_", filename)
    if not match:
        raise ValueError(
            f"Migration filename must start with numeric prefix: {filename}"
        )
    return int(match.group(1))


def ensure_schema(
    conn: sqlite3.Connection,
    lock_path: Optional[Path] = None,
) -> None:
    """
    Apply all pending migrations to *conn* in ascending version order.

    Safe to call on a fresh DB (no schema_meta table yet) and on an existing
    DB that is already at the latest version (no-op).

    Cross-process safety: acquires a file lock before checking schema version
    so concurrent callers (dashboard + daemon) serialise safely.
    """
    _lock_path = lock_path or _DEFAULT_LOCK_PATH

    # Acquire OS-level cross-process lock before any schema work.
    if sys.platform != "win32":
        import fcntl  # noqa: PLC0415
        _lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = open(_lock_path, "w")  # noqa: WPS515
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            _apply_migrations(conn)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
    else:
        # Windows: no fcntl; fall back to unguarded (single-process use)
        _apply_migrations(conn)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Inner migration loop, called with OS file lock already held."""
    # BEGIN EXCLUSIVE to prevent concurrent SQLite writers from interleaving
    # partial migration writes.
    conn.execute("BEGIN EXCLUSIVE")
    try:
        current_version = _read_current_version(conn)
        migration_files = sorted(
            [f for f in _MIGRATIONS_DIR.iterdir() if f.suffix == ".sql"],
            key=lambda f: _migration_version(f.name),
        )

        for migration_file in migration_files:
            version = _migration_version(migration_file.name)
            if version <= current_version:
                continue

            sql = migration_file.read_text(encoding="utf-8")

            # Strip PRAGMA statements from the top of 001_init.sql — SQLite
            # doesn't allow PRAGMAs inside transactions and the pragmas are
            # already applied by open_db() via _apply_pragmas().
            sql_for_exec = re.sub(
                r"^\s*PRAGMA\s+\S.*?;\s*", "", sql, flags=re.MULTILINE | re.IGNORECASE
            )

            conn.executescript(sql_for_exec)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(version),),
            )
            current_version = version

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def _read_current_version(conn: sqlite3.Connection) -> int:
    """Read the current schema version from schema_meta (0 if table absent)."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()

    if row is None:
        return 0

    version_row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if version_row is None:
        raise RuntimeError(
            "schema_meta table exists but schema_version row is missing. "
            "DB may be corrupted."
        )
    try:
        return int(version_row[0])
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"Unexpected schema_version value: '{version_row[0]}'. "
            "Expected a numeric string."
        ) from exc
