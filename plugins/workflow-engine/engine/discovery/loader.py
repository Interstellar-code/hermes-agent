"""
Workflow YAML discovery — finds and loads workflow YAML files from disk.

Search order (later wins on name collision):
  1. plugins/workflow-engine/defaults/*.yaml  (bundled, read-only)
  2. HERMES_HOME/workflows/*.yaml             (user)

Each file:
  1. Parse YAML
  2. Validate against WorkflowDefinition Pydantic model
  3. Compute SHA-256 content hash
  4. Upsert into workflow_definitions if hash changed

Mirrors the TS discovery/discovery.ts scope/precedence logic.
MAX_DISCOVERY_DEPTH = 1 (one subfolder level, same as TS constant).
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

from hermes_constants import get_hermes_home
from typing import Optional

from engine.schemas.workflow import WorkflowDefinition, WorkflowLoadError, WorkflowWithSource, WorkflowSource
from engine.discovery.validator import validate_workflow_yaml

MAX_DISCOVERY_DEPTH = 1

# Bundled defaults directory — sibling of this file's plugin root
_PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent
_BUNDLED_DEFAULTS_DIR = _PLUGIN_DIR / "defaults"

# User workflows directory
_USER_WORKFLOWS_DIR = get_hermes_home() / "workflows"


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _load_yaml_files_from_dir(
    dir_path: Path,
    depth: int = 0,
) -> list[tuple[str, str, Path]]:
    """
    Walk dir_path up to MAX_DISCOVERY_DEPTH, collecting (filename, content, path) tuples
    for .yaml/.yml files. Mirrors TS loadWorkflowsFromDir.
    """
    results: list[tuple[str, str, Path]] = []
    if not dir_path.exists():
        return results
    try:
        entries = list(dir_path.iterdir())
    except OSError:
        return results

    for entry in entries:
        if entry.is_dir():
            if depth < MAX_DISCOVERY_DEPTH:
                results.extend(_load_yaml_files_from_dir(entry, depth + 1))
        elif entry.suffix in (".yaml", ".yml"):
            try:
                content = entry.read_text(encoding="utf-8")
                results.append((entry.name, content, entry))
            except OSError:
                pass
    return results


def parse_workflow(
    content: str,
    filename: str,
) -> tuple[WorkflowDefinition | None, WorkflowLoadError | None]:
    """
    Parse and validate a single YAML string. Thin wrapper over validator.
    Returns (workflow, None) on success or (None, error) on failure.
    """
    from engine.discovery.validator import validate_workflow_yaml
    return validate_workflow_yaml(content, filename)


def _upsert_workflow(
    conn: sqlite3.Connection,
    workflow: WorkflowDefinition,
    source: WorkflowSource,
    scope_path: Optional[str],
    yaml_content: str,
    file_path: Path,
) -> None:
    """
    Upsert a workflow_definition row. Skips update if checksum unchanged.
    """
    checksum = _sha256(yaml_content)
    now_ms = int(time.time() * 1000)

    existing = conn.execute(
        "SELECT checksum FROM workflow_definitions WHERE id = ?",
        (workflow.id,),
    ).fetchone()

    if existing is not None:
        if existing["checksum"] == checksum:
            return  # no change
        conn.execute(
            """
            UPDATE workflow_definitions
               SET name=?, description=?, source=?, scope_path=?, yaml=?,
                   checksum=?, version=?, tags=?, updated_at=?, kind=?
             WHERE id=?
            """,
            (
                workflow.name,
                workflow.description,
                source,
                scope_path,
                yaml_content,
                checksum,
                workflow.version if hasattr(workflow, "version") else None,
                __import__("json").dumps(workflow.tags) if workflow.tags else None,
                now_ms,
                workflow.kind or "workflow",
                workflow.id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO workflow_definitions
              (id, name, description, source, scope_path, yaml, checksum,
               version, tags, created_at, updated_at, kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow.id,
                workflow.name,
                workflow.description,
                source,
                scope_path,
                yaml_content,
                checksum,
                workflow.version if hasattr(workflow, "version") else None,
                __import__("json").dumps(workflow.tags) if workflow.tags else None,
                now_ms,
                now_ms,
                workflow.kind or "workflow",
            ),
        )


def _workflow_id_from_filename(filename: str) -> str:
    """Derive a stable workflow id from its filename (strip extension, normalise)."""
    stem = Path(filename).stem
    return stem.lower().replace(" ", "-")


def discover_and_upsert(
    conn: sqlite3.Connection,
    extra_dirs: Optional[list[Path]] = None,
) -> tuple[list[WorkflowWithSource], list[WorkflowLoadError]]:
    """
    Discover workflow YAMLs from all sources, validate, and upsert into *conn*.

    Source precedence (later wins on name collision):
      bundled < user < extra_dirs

    Returns (loaded_workflows, errors).
    """
    import json

    sources: list[tuple[WorkflowSource, Path, Optional[str]]] = [
        ("bundled", _BUNDLED_DEFAULTS_DIR, None),
        ("global", _USER_WORKFLOWS_DIR, None),
    ]
    if extra_dirs:
        for d in extra_dirs:
            sources.append(("project", d, str(d)))

    loaded: list[WorkflowWithSource] = []
    errors: list[WorkflowLoadError] = []
    # Track by filename so later sources override earlier ones
    seen: dict[str, WorkflowWithSource] = {}

    for source, dir_path, scope_path in sources:
        for filename, content, file_path in _load_yaml_files_from_dir(dir_path):
            workflow, error = parse_workflow(content, filename)
            if error is not None:
                errors.append(error)
                continue

            assert workflow is not None

            # If no explicit id in YAML, derive from filename
            if workflow.id is None:
                # patch id — we need it for DB upsert
                object.__setattr__(workflow, "id", _workflow_id_from_filename(filename))

            with_source = WorkflowWithSource(workflow=workflow, source=source)
            seen[filename] = with_source

            try:
                _upsert_workflow(conn, workflow, source, scope_path, content, file_path)
            except Exception as exc:
                errors.append(WorkflowLoadError(
                    filename=filename,
                    error=f"DB upsert failed: {exc}",
                    errorType="validation_error",
                ))

    loaded = list(seen.values())
    return loaded, errors
