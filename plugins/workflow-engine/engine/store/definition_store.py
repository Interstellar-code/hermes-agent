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

logger = logging.getLogger("workflow.definition-store")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _row_to_def(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


class ConflictError(Exception):
    """Raised when a compare-and-swap write detects a concurrent modification."""


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
    ) -> Dict[str, Any]:
        """Parse yaml_text, validate, upsert, return the row dict.

        CR-1: when expected_checksum is provided and the row exists, uses
        a compare-and-swap WHERE clause.  Raises ConflictError on rowcount==0.
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

        self._conn.commit()
        return self.get_definition(definition_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    def delete_definition(self, definition_id: str) -> int:
        """Delete a non-bundled definition. Returns rows deleted (0 or 1)."""
        result = self._conn.execute(
            "DELETE FROM workflow_definitions WHERE id = ? AND source != 'bundled'",
            (definition_id,),
        )
        self._conn.commit()
        return result.rowcount

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
                        updated += 1
                else:
                    # bundled_checksum == file_sum and user_modified==0 — unchanged
                    skipped += 1

            except Exception:
                logger.exception("seed_bundled: error processing %s", yaml_file)
                errors += 1

        self._conn.commit()
        return {"inserted": inserted, "updated": updated, "skipped": skipped, "errors": errors}
