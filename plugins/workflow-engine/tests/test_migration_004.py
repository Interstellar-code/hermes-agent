"""Migration 004 — scheduled_runs table + workflow_runs new columns."""
from __future__ import annotations

from engine.db.client import open_db
from engine.db.migrate import ensure_schema


def test_workflow_runs_new_columns():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        cols = {
            r["name"]: r
            for r in conn.execute("PRAGMA table_info(workflow_runs)").fetchall()
        }
        assert "priority" in cols
        assert "max_runtime_s" in cols
        assert "scheduled_for" in cols
        # priority must default to 0
        assert cols["priority"]["dflt_value"] in ("0", 0)


def test_scheduled_runs_table_present():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scheduled_runs'"
        ).fetchall()
        assert len(rows) == 1
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(scheduled_runs)").fetchall()
        }
        expected = {
            "id", "workflow_id", "inputs_json", "trigger_json", "run_at",
            "priority", "max_runtime_s", "cron_expr", "status", "created_at",
        }
        assert expected.issubset(cols), f"missing: {expected - cols}"


def test_scheduled_runs_index():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        names = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_sr_due" in names


def test_idempotent_upgrade_preserves_rows():
    """Applying migrations again is a no-op and existing rows survive."""
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        # Insert a workflow_definition + workflow_run before re-running.
        conn.execute(
            "INSERT INTO workflow_definitions "
            "(id, name, source, yaml, checksum, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("w1", "W1", "user", "id: w1\nname: W1\nnodes: []\n", "x", 1, 1),
        )
        conn.execute(
            "INSERT INTO workflow_runs "
            "(id, workflow_id, conversation_id, working_path, user_message, "
            "status, current_phase, started_at, last_heartbeat) "
            "VALUES (?, ?, ?, ?, ?, 'pending', 'plan', ?, ?)",
            ("r1", "w1", "c1", "/tmp", "go", 1, 1),
        )
        conn.commit()
        ensure_schema(conn)  # idempotent
        row = conn.execute(
            "SELECT priority, max_runtime_s, scheduled_for FROM workflow_runs WHERE id = 'r1'"
        ).fetchone()
        assert row["priority"] == 0
        assert row["max_runtime_s"] is None
        assert row["scheduled_for"] is None


def _seed_def_run(conn, source="user", status="failed"):
    conn.execute(
        "INSERT INTO workflow_definitions "
        "(id, name, source, yaml, checksum, created_at, updated_at) "
        "VALUES ('w1', 'W1', ?, 'x', 'x', 1, 1)",
        (source,),
    )
    conn.execute(
        "INSERT INTO workflow_runs "
        "(id, workflow_id, conversation_id, working_path, user_message, "
        "status, current_phase, started_at, last_heartbeat) "
        "VALUES ('r1', 'w1', 'c1', '/tmp', 'go', ?, 'plan', 1, 1)",
        (status,),
    )
    conn.execute(
        "INSERT INTO node_runs (id, workflow_run_id, dag_node_id, node_type, status, started_at) "
        "VALUES ('nr1', 'r1', 'n', 'prompt', 'failed', 1)"
    )
    conn.commit()


def _count(conn, t):
    return conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]


def test_delete_definition_with_run_history():
    """#250: deleting a definition cascades runs/node_runs/scheduled_runs."""
    from engine.store.definition_store import DefinitionStore

    with open_db(":memory:") as conn:
        ensure_schema(conn)
        _seed_def_run(conn)
        conn.execute(
            "INSERT INTO scheduled_runs (id, workflow_id, inputs_json, trigger_json, run_at, created_at) "
            "VALUES ('s1', 'w1', '{}', '{}', 'now', 'now')"
        )
        conn.commit()
        assert DefinitionStore(conn).delete_definition("w1") == 1
        for t in ("workflow_definitions", "workflow_runs", "node_runs", "scheduled_runs"):
            assert _count(conn, t) == 0


def test_delete_definition_refused_with_active_run():
    import pytest
    from engine.store.definition_store import ConflictError, DefinitionStore

    with open_db(":memory:") as conn:
        ensure_schema(conn)
        _seed_def_run(conn, status="running")
        with pytest.raises(ConflictError):
            DefinitionStore(conn).delete_definition("w1")
        assert _count(conn, "workflow_runs") == 1 and _count(conn, "workflow_definitions") == 1


def test_delete_bundled_definition_noop():
    from engine.store.definition_store import DefinitionStore

    with open_db(":memory:") as conn:
        ensure_schema(conn)
        _seed_def_run(conn, source="bundled")
        assert DefinitionStore(conn).delete_definition("w1") == 0
        assert _count(conn, "workflow_runs") == 1 and _count(conn, "node_runs") == 1
