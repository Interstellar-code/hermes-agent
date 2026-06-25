"""
DB connection factory for the workflow engine.

Usage:
    from engine.db.client import open_db

    with open_db("/path/to/workflow.db") as conn:
        rows = conn.execute("SELECT * FROM workflow_definitions").fetchall()

For in-memory DBs (tests):
    with open_db(":memory:") as conn:
        ...

Pragmas applied on every connection:
    journal_mode = WAL
    foreign_keys = ON
    synchronous = NORMAL
    busy_timeout = 5000
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row


@contextmanager
def open_db(db_path: str = "") -> Generator[sqlite3.Connection, None, None]:
    """
    Open a SQLite connection, apply pragmas, yield, then close.

    :memory: paths skip file creation — each call returns a fresh connection
    so multiple test fixtures in the same file don't share state.

    For file-based paths the directory is created if it does not exist.
    """
    if db_path == ":memory:" or not db_path:
        conn = sqlite3.connect(":memory:")
        _apply_pragmas(conn)
        try:
            yield conn
        finally:
            conn.close()
        return

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    _apply_pragmas(conn)
    try:
        yield conn
    finally:
        conn.close()
