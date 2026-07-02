"""Tests for DefinitionStore.seed_bundled() provenance-gated logic."""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from engine.db.client import open_db
from engine.db.migrate import ensure_schema
from engine.store.definition_store import DefinitionStore, _sha256


_YAML_A = """\
id: wf-alpha
name: Alpha
description: First version
nodes:
  - id: step1
    prompt: Do step 1
"""

_YAML_A_V2 = """\
id: wf-alpha
name: Alpha v2
description: Upgraded version
nodes:
  - id: step1
    prompt: Do step 1 upgraded
"""

_YAML_B = """\
id: wf-beta
name: Beta
nodes:
  - id: step1
    prompt: Do step 1
"""


def _make_store(conn):
    return DefinitionStore(conn)


def _write_yaml(tmp_dir: Path, filename: str, content: str) -> None:
    (tmp_dir / filename).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Branch 1: INSERT (id absent)
# ---------------------------------------------------------------------------

def test_insert_new_bundled_workflow():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        store = _make_store(conn)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_yaml(d, "wf-alpha.yaml", _YAML_A)
            result = store.seed_bundled(d)

        assert result["inserted"] == 1
        assert result["updated"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == 0

        row = conn.execute(
            "SELECT * FROM workflow_definitions WHERE id='wf-alpha'"
        ).fetchone()
        assert row is not None
        assert row["source"] == "bundled"
        assert row["user_modified"] == 0
        assert row["bundled_checksum"] == _sha256(_YAML_A)
        assert row["checksum"] == _sha256(_YAML_A)


# ---------------------------------------------------------------------------
# Branch 2: factory upgraded upstream, user_modified=0 → UPDATE
# ---------------------------------------------------------------------------

def test_factory_upgrade_when_unmodified():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        store = _make_store(conn)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_yaml(d, "wf-alpha.yaml", _YAML_A)
            store.seed_bundled(d)

            # Now factory upgrades the file
            _write_yaml(d, "wf-alpha.yaml", _YAML_A_V2)
            result = store.seed_bundled(d)

        assert result["updated"] == 1
        assert result["skipped"] == 0

        row = conn.execute(
            "SELECT * FROM workflow_definitions WHERE id='wf-alpha'"
        ).fetchone()
        assert row["yaml"] == _YAML_A_V2
        assert row["checksum"] == _sha256(_YAML_A_V2)
        assert row["bundled_checksum"] == _sha256(_YAML_A_V2)
        assert row["user_modified"] == 0


# ---------------------------------------------------------------------------
# Branch 3: user_modified=1 → SKIP
# ---------------------------------------------------------------------------

def test_skip_when_user_modified():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        store = _make_store(conn)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_yaml(d, "wf-alpha.yaml", _YAML_A)
            store.seed_bundled(d)

            # Simulate user edit
            conn.execute(
                "UPDATE workflow_definitions SET user_modified=1, yaml=? WHERE id='wf-alpha'",
                ("id: wf-alpha\nname: User Edited\nnodes: []\n",),
            )
            conn.commit()

            # Factory upgrades the file — should still skip
            _write_yaml(d, "wf-alpha.yaml", _YAML_A_V2)
            result = store.seed_bundled(d)

        assert result["skipped"] == 1
        assert result["updated"] == 0

        row = conn.execute(
            "SELECT yaml FROM workflow_definitions WHERE id='wf-alpha'"
        ).fetchone()
        # User's yaml preserved
        assert "User Edited" in row["yaml"]


# ---------------------------------------------------------------------------
# CR-2 reconciliation: bundled_checksum IS NULL, stored yaml == file → clean
# ---------------------------------------------------------------------------

def test_reconciliation_null_bundled_checksum_clean():
    """First boot after migration: stored yaml matches factory file → factory-clean."""
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        # Insert a bundled row as if pre-005 (bundled_checksum NULL)
        checksum = _sha256(_YAML_A)
        conn.execute(
            "INSERT INTO workflow_definitions "
            "(id, name, source, yaml, checksum, user_modified, bundled_checksum, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("wf-alpha", "Alpha", "bundled", _YAML_A, checksum, 0, None, 1, 1),
        )
        conn.commit()

        store = _make_store(conn)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_yaml(d, "wf-alpha.yaml", _YAML_A)
            result = store.seed_bundled(d)

        # No insert/update — just reconciliation + skip
        assert result["inserted"] == 0
        assert result["updated"] == 0
        assert result["skipped"] == 1

        row = conn.execute(
            "SELECT * FROM workflow_definitions WHERE id='wf-alpha'"
        ).fetchone()
        assert row["bundled_checksum"] == _sha256(_YAML_A)
        assert row["user_modified"] == 0


# ---------------------------------------------------------------------------
# CR-2 reconciliation: bundled_checksum IS NULL, stored yaml != file → diverged
# ---------------------------------------------------------------------------

def test_reconciliation_null_bundled_checksum_divergent():
    """First boot after migration: stored yaml differs from factory → user_modified=1."""
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        user_yaml = "id: wf-alpha\nname: My Custom Alpha\nnodes: []\n"
        conn.execute(
            "INSERT INTO workflow_definitions "
            "(id, name, source, yaml, checksum, user_modified, bundled_checksum, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("wf-alpha", "My Custom Alpha", "bundled", user_yaml, _sha256(user_yaml), 0, None, 1, 1),
        )
        conn.commit()

        store = _make_store(conn)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_yaml(d, "wf-alpha.yaml", _YAML_A)
            result = store.seed_bundled(d)

        # Diverged → user_modified=1, yaml preserved
        assert result["skipped"] == 1
        assert result["updated"] == 0

        row = conn.execute(
            "SELECT * FROM workflow_definitions WHERE id='wf-alpha'"
        ).fetchone()
        assert row["user_modified"] == 1
        # bundled_checksum set to current factory file sum
        assert row["bundled_checksum"] == _sha256(_YAML_A)
        # yaml is still the user's yaml
        assert "My Custom Alpha" in row["yaml"]


# ---------------------------------------------------------------------------
# CAS conflict: rowcount==0 path
# ---------------------------------------------------------------------------

def test_cas_sql_rowcount_zero_on_stale_where():
    """Verify the CAS UPDATE WHERE clause returns rowcount=0 when bundled_checksum doesn't match.

    This tests the SQL semantics that seed_bundled relies on for conflict detection.
    """
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        now = 1
        conn.execute(
            "INSERT INTO workflow_definitions "
            "(id, name, description, source, yaml, checksum, bundled_checksum, user_modified, created_at, updated_at, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("wf-alpha", "Alpha", "desc", "bundled", _YAML_A, _sha256(_YAML_A),
             _sha256(_YAML_A), 0, now, now, "workflow"),
        )
        conn.commit()

        # CAS UPDATE with stale bundled_checksum → rowcount should be 0
        stale = "old-value-that-does-not-match"
        result = conn.execute(
            """UPDATE workflow_definitions
                  SET yaml=?, checksum=?, bundled_checksum=?, updated_at=?
                WHERE id=? AND user_modified=0 AND bundled_checksum=?""",
            (_YAML_A_V2, _sha256(_YAML_A_V2), _sha256(_YAML_A_V2), 2,
             "wf-alpha", stale),
        )
        conn.commit()
        assert result.rowcount == 0, "CAS should miss when bundled_checksum doesn't match"

        # Original row unchanged
        row = conn.execute(
            "SELECT yaml FROM workflow_definitions WHERE id='wf-alpha'"
        ).fetchone()
        assert row["yaml"] == _YAML_A


# ---------------------------------------------------------------------------
# Empty directory
# ---------------------------------------------------------------------------

def test_empty_bundled_dir():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        store = _make_store(conn)
        with tempfile.TemporaryDirectory() as tmp:
            result = store.seed_bundled(Path(tmp))
        assert result == {"inserted": 0, "updated": 0, "skipped": 0, "errors": 0}


def test_nonexistent_bundled_dir():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        store = _make_store(conn)
        result = store.seed_bundled(Path("/nonexistent/path/xyz"))
        assert result == {"inserted": 0, "updated": 0, "skipped": 0, "errors": 0}
