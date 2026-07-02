"""
test_db_schema — verifies that ensure_schema() creates the expected tables and indexes.
"""
import sqlite3
import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from engine.db.client import open_db
from engine.db.migrate import ensure_schema

EXPECTED_TABLES = {
    "workflow_definitions",
    "workflow_runs",
    "phase_transitions",
    "node_runs",
    "workflow_events",
    "gateway_event_cursor",
    "schema_meta",
}


def test_ensure_schema_creates_all_tables():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r["name"] for r in rows}
        assert EXPECTED_TABLES.issubset(table_names), (
            f"Missing tables: {EXPECTED_TABLES - table_names}"
        )


def test_schema_version_is_set():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        assert row is not None
        assert int(row["value"]) >= 1


def test_idempotent_double_run():
    """Calling ensure_schema twice on the same DB must not raise."""
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        ensure_schema(conn)
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        assert row is not None


def test_workflow_definitions_columns():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        cols = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(workflow_definitions)"
            ).fetchall()
        }
        expected = {
            "id", "name", "description", "source", "scope_path",
            "yaml", "checksum", "version", "tags",
            "created_at", "updated_at", "kind",
        }
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"


def test_node_runs_columns():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(node_runs)").fetchall()
        }
        # key columns from both migrations
        for col in ["id", "workflow_run_id", "dag_node_id", "node_type",
                    "status", "parent_subgraph_node_run_id"]:
            assert col in cols, f"Column '{col}' missing from node_runs"


def test_subgraph_columns_from_migration_002():
    """Migration 002 adds kind to workflow_definitions and parent_subgraph_node_run_id to node_runs."""
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        wd_cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(workflow_definitions)").fetchall()
        }
        assert "kind" in wd_cols

        nr_cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(node_runs)").fetchall()
        }
        assert "parent_subgraph_node_run_id" in nr_cols
