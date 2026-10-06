"""
DefinitionStore — CRUD for workflow_definitions table.

Wraps raw sqlite3.Connection. All methods are synchronous (SQLite is sync).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine.schemas.workflow import WorkflowDefinition, WorkflowSource
from engine.discovery.validator import validate_workflow_yaml
from engine.store.run_store import locked

logger = logging.getLogger("workflow.definition-store")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


# Version history retention, per workflow: keep the newest SNAPSHOT_KEEP
# snapshots no run references. Run-referenced ones are always kept and do not
# use up slots. Rows (re)pinned in the last SNAPSHOT_GRACE_MS are never deleted
# (another process may be between pinning a snapshot and inserting its run).
SNAPSHOT_KEEP = 50
SNAPSHOT_GRACE_MS = 10 * 60_000


def _row_to_def(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


class ConflictError(Exception):
    """Raised when a compare-and-swap write detects a concurrent modification."""


@locked
class DefinitionStore:
    """CRUD operations over workflow_definitions."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    def list_definitions(
        self,
        *,
        source: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if source:
            clauses.append("source = ?")
            params.append(source)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM workflow_definitions {where} ORDER BY name LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_def(r) for r in rows]

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------

    def get_definition(self, definition_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM workflow_definitions WHERE id = ?",
            (definition_id,),
        ).fetchone()
        return _row_to_def(row) if row else None

    # ------------------------------------------------------------------
    # version history (workflow_definition_snapshots)
    # ------------------------------------------------------------------

    def list_snapshots(self, workflow_id: str) -> List[Dict[str, Any]]:
        """Every snapshot of ``workflow_id``, newest first, with the number of
        runs pinned to it as their definition or a subgraph (``in_use_by_runs``)."""
        rows = self._conn.execute(
            """
            SELECT s.checksum, s.version, s.saved_at, s.source, s.yaml,
                   (SELECT COUNT(*) FROM workflow_runs r
                     WHERE (r.workflow_id = s.workflow_id AND r.definition_checksum = s.checksum)
                        OR EXISTS (
                             SELECT 1 FROM json_each(
                                    CASE WHEN json_valid(r.metadata) THEN r.metadata ELSE '{}' END,
                                    '$.subgraph_pins') p
                              WHERE p.key = s.workflow_id AND p.value = s.checksum)
                   ) AS in_use_by_runs
              FROM workflow_definition_snapshots s
             WHERE s.workflow_id = ?
             ORDER BY COALESCE(s.saved_at, s.created_at) DESC, s.checksum DESC
            """,
            (workflow_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _snapshot(self, definition_id: str, source: str) -> None:
        """Record the row's current YAML as a version, then apply retention.

        Runs inside the caller's save transaction (the caller commits). Keyed
        by the same ``_sha256`` the runner pins runs with, so a save and a run
        of identical YAML share one row. A re-save of an existing version only
        moves its ``saved_at``/``source``; ``created_at`` (the run-pin time the
        age sweep uses) is left alone."""
        row = self._conn.execute(
            "SELECT yaml, version FROM workflow_definitions WHERE id = ?", (definition_id,),
        ).fetchone()
        if row is None:
            return
        now = _now_ms()
        self._conn.execute(
            "INSERT INTO workflow_definition_snapshots "
            "(workflow_id, checksum, version, yaml, created_at, saved_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (workflow_id, checksum) DO UPDATE "
            "SET saved_at = excluded.saved_at, source = excluded.source",
            (definition_id, _sha256(row["yaml"]), row["version"], row["yaml"], now, now, source),
        )
        # Retention: delete only snapshots no run pins (as its definition or as
        # a subgraph) that rank beyond SNAPSHOT_KEEP among the unpinned ones.
        # ponytail: scans workflow_runs.metadata once per save; fine while runs
        # are pruned by retention_days, index the pins if that ever stops.
        self._conn.execute(
            """
            WITH pinned(checksum) AS (
                SELECT definition_checksum FROM workflow_runs
                 WHERE workflow_id = :wf AND definition_checksum IS NOT NULL
                UNION
                SELECT p.value FROM workflow_runs r, json_each(
                       CASE WHEN json_valid(r.metadata) THEN r.metadata ELSE '{}' END,
                       '$.subgraph_pins') p
                 WHERE p.key = :wf AND p.value IS NOT NULL
            )
            DELETE FROM workflow_definition_snapshots
             WHERE workflow_id = :wf
               AND created_at < :grace
               AND checksum IN (
                     SELECT checksum FROM workflow_definition_snapshots
                      WHERE workflow_id = :wf
                        AND checksum NOT IN (SELECT checksum FROM pinned)
                      ORDER BY COALESCE(saved_at, created_at) DESC, checksum DESC
                      LIMIT -1 OFFSET :keep)
            """,
            {"wf": definition_id, "grace": now - SNAPSHOT_GRACE_MS, "keep": SNAPSHOT_KEEP},
        )

    # ------------------------------------------------------------------
    # upsert (user/project rows)
    # ------------------------------------------------------------------

    def upsert_definition(
        self,
        *,
        definition_id: str,
        yaml_text: str,
        source: WorkflowSource = "user",
        source_path: Optional[str] = None,
        expected_checksum: Optional[str] = None,
        snapshot_source: str = "save",
    ) -> Dict[str, Any]:
        """Parse yaml_text, validate, upsert, return the row dict.

        CR-1: when expected_checksum is provided and the row exists, uses
        a compare-and-swap WHERE clause.  Raises ConflictError on rowcount==0.
        ``snapshot_source`` labels the version snapshot ('save' or 'import').
        """
        workflow, error = validate_workflow_yaml(yaml_text, source_path or "<inline>")
        if error or workflow is None:
            raise ValueError(f"Invalid workflow YAML: {error.error if error else 'unknown'}")

        if not definition_id or not isinstance(definition_id, str):
            raise ValueError("definition_id is required")
        object.__setattr__(workflow, "id", definition_id)

        checksum = _sha256(yaml_text)
        now = _now_ms()

        existing = self._conn.execute(
            "SELECT checksum FROM workflow_definitions WHERE id = ?",
            (workflow.id,),
        ).fetchone()

        if existing is not None:
            if existing["checksum"] == checksum and expected_checksum is None:
                return self.get_definition(workflow.id)  # type: ignore[return-value]

            if expected_checksum is not None:
                # CR-1: CAS update — only proceed if checksum matches
                result = self._conn.execute(
                    """
                    UPDATE workflow_definitions
                       SET name=?, description=?, source=?, scope_path=?, yaml=?,
                           checksum=?, updated_at=?, kind=?
                     WHERE id=? AND checksum=?
                    """,
                    (
                        workflow.name,
                        workflow.description,
                        source,
                        source_path,
                        yaml_text,
                        checksum,
                        now,
                        workflow.kind or "workflow",
                        workflow.id,
                        expected_checksum,
                    ),
                )
                if result.rowcount == 0:
                    raise ConflictError(
                        f"Checksum mismatch for {workflow.id!r}: expected {expected_checksum!r}"
                    )
            else:
                self._conn.execute(
                    """
                    UPDATE workflow_definitions
                       SET name=?, description=?, source=?, scope_path=?, yaml=?,
                           checksum=?, updated_at=?, kind=?
                     WHERE id=?
                    """,
                    (
                        workflow.name,
                        workflow.description,
                        source,
                        source_path,
                        yaml_text,
                        checksum,
                        now,
                        workflow.kind or "workflow",
                        workflow.id,
                    ),
                )
        else:
            self._conn.execute(
                """
                INSERT INTO workflow_definitions
                  (id, name, description, source, scope_path, yaml, checksum,
                   created_at, updated_at, kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow.id,
                    workflow.name,
                    workflow.description,
                    source,
                    source_path,
                    yaml_text,
                    checksum,
                    now,
                    now,
                    workflow.kind or "workflow",
                ),
            )
        self._snapshot(workflow.id, snapshot_source)
        self._conn.commit()
        return self.get_definition(workflow.id)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # mark_user_edit — edit a bundled row in-place (Phase 3)
    # ------------------------------------------------------------------

    def mark_user_edit(
        self,
        definition_id: str,
        yaml_text: str,
        expected_checksum: Optional[str] = None,
        snapshot_source: str = "save",
    ) -> Dict[str, Any]:
        """Edit a bundled workflow row.  Keeps source='bundled', sets user_modified=1.

        CR-1: When expected_checksum is provided, uses CAS WHERE checksum=?.
        Raises ConflictError on stale checksum; ValueError if row not found / not bundled.
        """
        workflow, error = validate_workflow_yaml(yaml_text, "<inline>")
        if error or workflow is None:
            raise ValueError(f"Invalid workflow YAML: {error.error if error else 'unknown'}")

        checksum = _sha256(yaml_text)
        now = _now_ms()

        if expected_checksum is not None:
            result = self._conn.execute(
                """
                UPDATE workflow_definitions
                   SET name=?, description=?, yaml=?, checksum=?, user_modified=1,
                       updated_at=?, kind=?
                 WHERE id=? AND source='bundled' AND checksum=?
                """,
                (
                    workflow.name,
                    workflow.description,
                    yaml_text,
                    checksum,
                    now,
                    workflow.kind or "workflow",
                    definition_id,
                    expected_checksum,
                ),
            )
            if result.rowcount == 0:
                # Distinguish conflict vs not-found/not-bundled
                row = self._conn.execute(
                    "SELECT source FROM workflow_definitions WHERE id = ?",
                    (definition_id,),
                ).fetchone()
                if row is None or row["source"] != "bundled":
                    raise ValueError(f"Not a bundled row or not found: {definition_id!r}")
                raise ConflictError(
                    f"Checksum mismatch for {definition_id!r}: expected {expected_checksum!r}"
                )
        else:
            result = self._conn.execute(
                """
                UPDATE workflow_definitions
                   SET name=?, description=?, yaml=?, checksum=?, user_modified=1,
                       updated_at=?, kind=?
                 WHERE id=? AND source='bundled'
                """,
                (
                    workflow.name,
                    workflow.description,
                    yaml_text,
                    checksum,
                    now,
                    workflow.kind or "workflow",
                    definition_id,
                ),
            )
            if result.rowcount == 0:
                raise ValueError(f"Not a bundled row or not found: {definition_id!r}")

        self._snapshot(definition_id, snapshot_source)
        self._conn.commit()
        return self.get_definition(definition_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # reset_to_factory — reset a bundled row to factory yaml (Phase 3)
    # ------------------------------------------------------------------

    def reset_to_factory(
        self,
        definition_id: str,
        factory_yaml: str,
    ) -> Dict[str, Any]:
        """Reset a bundled row to the factory yaml.  Clears user_modified.

        Raises ValueError if the row doesn't exist or isn't source='bundled'.
        """
        workflow, error = validate_workflow_yaml(factory_yaml, "<factory>")
        if error or workflow is None:
            raise ValueError(f"Invalid factory YAML: {error.error if error else 'unknown'}")

        checksum = _sha256(factory_yaml)
        now = _now_ms()

        result = self._conn.execute(
            """
            UPDATE workflow_definitions
               SET yaml=?, checksum=?, bundled_checksum=?, user_modified=0, updated_at=?,
                   name=?, description=?, kind=?
             WHERE id=? AND source='bundled'
            """,
            (
                factory_yaml,
                checksum,
                checksum,
                now,
                workflow.name,
                workflow.description,
                workflow.kind or "workflow",
                definition_id,
            ),
        )
        if result.rowcount == 0:
            raise ValueError(f"Not a bundled row or not found: {definition_id!r}")

        self._snapshot(definition_id, "reset")
        self._conn.commit()
        return self.get_definition(definition_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    def delete_definition(self, definition_id: str) -> int:
        """Delete a non-bundled definition and its run history. Returns rows deleted (0 or 1).

        workflow_runs.workflow_id has no ON DELETE action (001_init.sql), so runs
        (node_runs/events/transitions cascade from them) and scheduled_runs are
        removed first, in the same transaction.
        """
        try:
            ok = self._conn.execute(
                "SELECT 1 FROM workflow_definitions WHERE id = ? AND source != 'bundled'",
                (definition_id,),
            ).fetchone()
            if not ok:
                return 0
            if self._conn.execute(
                "SELECT 1 FROM workflow_runs WHERE workflow_id = ? "
                "AND status IN ('pending', 'running', 'paused')",
                (definition_id,),
            ).fetchone():
                raise ConflictError(
                    f"definition {definition_id!r} has active runs; cancel them first"
                )
            self._conn.execute("DELETE FROM workflow_runs WHERE workflow_id = ?", (definition_id,))
            self._conn.execute("DELETE FROM scheduled_runs WHERE workflow_id = ?", (definition_id,))
            # Its version history goes too (else a recreated id inherits it),
            # except snapshots other workflows' runs still pin as a subgraph.
            self._conn.execute(
                """
                DELETE FROM workflow_definition_snapshots
                 WHERE workflow_id = ?
                   AND NOT EXISTS (
                         SELECT 1 FROM workflow_runs r, json_each(
                                CASE WHEN json_valid(r.metadata) THEN r.metadata ELSE '{}' END,
                                '$.subgraph_pins') p
                          WHERE p.key = workflow_definition_snapshots.workflow_id
                            AND p.value = workflow_definition_snapshots.checksum)
                """,
                (definition_id,),
            )
            result = self._conn.execute(
                "DELETE FROM workflow_definitions WHERE id = ?", (definition_id,)
            )
            self._conn.commit()
            return result.rowcount
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    # seed bundled (Phase 2 — provenance-gated + CAS)
    # ------------------------------------------------------------------

    def seed_bundled(self, bundled_dir: Path) -> Dict[str, int]:
        """Upsert all *.yaml files from bundled_dir using provenance-gated logic.

        Decision matrix (CR-1, CR-2):
        - id absent → INSERT: source='bundled', user_modified=0, bundled_checksum=file_sum
        - id present, bundled_checksum IS NULL (first boot after migration — CR-2 reconciliation):
            if checksum == file_sum → factory-clean: set bundled_checksum=file_sum, user_modified=0
            else                   → user diverged: set bundled_checksum=file_sum, user_modified=1
            then apply decision below with reconciled values.
        - decision (with reconciled bundled_checksum/user_modified):
            user_modified == 1                → SKIP (preserve user edit)
            bundled_checksum != file_sum      → CAS UPDATE (factory upgraded upstream)
            else (bundled_checksum == file_sum, user_modified==0) → SKIP (unchanged)

        Returns {"inserted", "updated", "skipped", "errors"}.
        """
        inserted = updated = skipped = errors = 0
        if not bundled_dir.exists():
            return {"inserted": 0, "updated": 0, "skipped": 0, "errors": 0}

        for yaml_file in sorted(bundled_dir.glob("*.yaml")):
            # Per file: a failure (e.g. in _snapshot) undoes that file's writes
            # only, so a definition never lands without its snapshot.
            self._conn.execute("SAVEPOINT seed_file")
            try:
                content = yaml_file.read_text(encoding="utf-8")
                workflow, error = validate_workflow_yaml(content, yaml_file.name)
                if error or not workflow:
                    errors += 1
                    continue
                if workflow.id is None:
                    object.__setattr__(workflow, "id", yaml_file.stem.lower().replace(" ", "-"))

                file_sum = _sha256(content)
                now = _now_ms()

                existing = self._conn.execute(
                    "SELECT checksum, user_modified, bundled_checksum FROM workflow_definitions WHERE id = ?",
                    (workflow.id,),
                ).fetchone()

                if existing is None:
                    # INSERT: new factory workflow
                    self._conn.execute(
                        """INSERT INTO workflow_definitions
                             (id, name, description, source, yaml, checksum,
                              bundled_checksum, user_modified, created_at, updated_at, kind)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            workflow.id, workflow.name, workflow.description,
                            "bundled", content, file_sum,
                            file_sum, 0, now, now, workflow.kind or "workflow",
                        ),
                    )
                    self._snapshot(workflow.id, "seed")
                    inserted += 1
                    continue

                # Existing row — check for CR-2 reconciliation (bundled_checksum IS NULL)
                ex_bundled_checksum = existing["bundled_checksum"]
                ex_user_modified = existing["user_modified"]
                ex_checksum = existing["checksum"]

                if ex_bundled_checksum is None:
                    # First boot after migration: reconcile
                    if ex_checksum == file_sum:
                        # Factory-clean: stored yaml matches factory file
                        self._conn.execute(
                            "UPDATE workflow_definitions SET bundled_checksum=?, user_modified=0 WHERE id=?",
                            (file_sum, workflow.id),
                        )
                        ex_bundled_checksum = file_sum
                        ex_user_modified = 0
                    else:
                        # Diverged: conservatively treat as user-modified
                        self._conn.execute(
                            "UPDATE workflow_definitions SET bundled_checksum=?, user_modified=1 WHERE id=?",
                            (file_sum, workflow.id),
                        )
                        ex_bundled_checksum = file_sum
                        ex_user_modified = 1

                # Decision
                if ex_user_modified == 1:
                    skipped += 1
                    continue

                if ex_bundled_checksum != file_sum:
                    # Factory upgraded upstream, user hasn't touched it — CAS UPDATE
                    result = self._conn.execute(
                        """UPDATE workflow_definitions
                              SET name=?, description=?, yaml=?, checksum=?,
                                  bundled_checksum=?, updated_at=?, kind=?
                            WHERE id=? AND user_modified=0 AND bundled_checksum=?""",
                        (
                            workflow.name, workflow.description, content, file_sum,
                            file_sum, now, workflow.kind or "workflow",
                            workflow.id, ex_bundled_checksum,
                        ),
                    )
                    if result.rowcount == 0:
                        logger.warning(
                            "seed_bundled: CAS conflict on %r — concurrent writer changed row; skipping",
                            workflow.id,
                        )
                        skipped += 1
                    else:
                        self._snapshot(workflow.id, "seed")
                        updated += 1
                else:
                    # bundled_checksum == file_sum and user_modified==0 — unchanged
                    skipped += 1

            except Exception:
                self._conn.execute("ROLLBACK TO seed_file")
                logger.exception("seed_bundled: error processing %s", yaml_file)
                errors += 1
            finally:
                self._conn.execute("RELEASE seed_file")

        self._conn.commit()
        return {"inserted": inserted, "updated": updated, "skipped": skipped, "errors": errors}
