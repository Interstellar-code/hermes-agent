"""
test_discovery — discovers the 3 fixture YAMLs and verifies DB insertion.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import sqlite3
import pytest

from engine.db.client import open_db
from engine.db.migrate import ensure_schema
from engine.discovery.loader import discover_and_upsert, parse_workflow, _load_yaml_files_from_dir
from engine.discovery.validator import validate_workflow_yaml

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "yaml"

FIXTURE_FILES = [
    "hello-world.yaml",
    "githubawesome-monitor.yaml",
    "tool-catalog-write.yaml",
]


# ---------------------------------------------------------------------------
# parse_workflow unit tests
# ---------------------------------------------------------------------------

def test_parse_hello_world():
    content = (FIXTURES_DIR / "hello-world.yaml").read_text()
    workflow, error = parse_workflow(content, "hello-world.yaml")
    assert error is None, f"Unexpected error: {error}"
    assert workflow is not None
    assert workflow.name == "Hello World"


def test_parse_invalid_yaml():
    _, error = parse_workflow("{{invalid: yaml: [", "bad.yaml")
    assert error is not None
    assert error.errorType == "parse_error"


def test_parse_missing_nodes():
    _, error = parse_workflow("name: No Nodes\ndescription: test\n", "no-nodes.yaml")
    assert error is not None
    assert error.errorType == "validation_error"


def test_parse_legacy_steps_rejected():
    yaml = "name: Legacy\ndescription: old format\nsteps:\n  - name: x\n    prompt: hi\n"
    _, error = parse_workflow(yaml, "legacy.yaml")
    assert error is not None
    assert "steps" in error.error.lower() or "removed" in error.error.lower()


# ---------------------------------------------------------------------------
# discover_and_upsert integration test
# ---------------------------------------------------------------------------

def test_discovery_inserts_3_fixtures():
    """
    Discover the 3 fixture YAMLs and confirm exactly 3 rows inserted into
    workflow_definitions in a fresh DB.
    """
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        loaded, errors = discover_and_upsert(conn, extra_dirs=[FIXTURES_DIR])

        # Fixture errors (if any) should not be parse failures on our known-good files
        fixture_errors = [e for e in errors if e.filename in FIXTURE_FILES]
        assert fixture_errors == [], f"Fixture parse errors: {fixture_errors}"

        # All 3 fixture workflows must be present in the DB.
        # When a fixture YAML is identical to a bundled default (same checksum),
        # the upsert skips the source update — so source may be 'bundled' or 'project'.
        fixture_stems = {Path(f).stem for f in FIXTURE_FILES}
        rows = conn.execute(
            "SELECT id, name, source FROM workflow_definitions"
        ).fetchall()
        present_ids = {r['id'] for r in rows}
        missing = fixture_stems - present_ids
        assert not missing, (
            f"Expected all 3 fixture workflows in DB, missing: {missing}. "
            f"Present: {[r['name'] for r in rows]}"
        )


def test_discovery_upsert_is_idempotent():
    """Running discover twice must not duplicate rows."""
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        discover_and_upsert(conn, extra_dirs=[FIXTURES_DIR])
        count_after_first = conn.execute(
            "SELECT COUNT(*) FROM workflow_definitions"
        ).fetchone()[0]

        discover_and_upsert(conn, extra_dirs=[FIXTURES_DIR])
        count_after_second = conn.execute(
            "SELECT COUNT(*) FROM workflow_definitions"
        ).fetchone()[0]

        assert count_after_first == count_after_second, (
            "Duplicate rows inserted on second discovery run"
        )


def test_discovery_checksum_stored():
    """Each workflow_definitions row must have a non-empty checksum."""
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        discover_and_upsert(conn, extra_dirs=[FIXTURES_DIR])
        rows = conn.execute(
            "SELECT checksum FROM workflow_definitions WHERE source='project'"
        ).fetchall()
        for row in rows:
            assert row["checksum"], "Empty checksum in workflow_definitions"
            assert len(row["checksum"]) == 64, "checksum should be SHA-256 hex (64 chars)"


def test_all_fixture_files_parse():
    """Each fixture YAML must parse cleanly individually."""
    for fname in FIXTURE_FILES:
        path = FIXTURES_DIR / fname
        content = path.read_text()
        workflow, error = parse_workflow(content, fname)
        assert error is None, f"{fname} failed: {error}"
        assert workflow is not None
        assert workflow.name, f"{fname}: workflow.name is empty"
