"""
Composition root — builds a WorkflowEngine with all dependencies wired.

create_engine(db_path) is the single entry point called at plugin load time.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from hermes_constants import get_hermes_home
from typing import Optional

from engine.db.client import open_db
from engine.db.migrate import ensure_schema
from engine.store.run_store import RunStore
from engine.store.definition_store import DefinitionStore
from engine.emitter.bus import EventBus
from engine.runtime.runner import WorkflowRunner
from engine.runtime.manifest import ManifestWriter
from engine.runtime.resume import mark_crashed_runs
from engine.runtime.seed_defaults import seed_defaults

logger = logging.getLogger("workflow.wiring")

_DEFAULT_DB_PATH = str(get_hermes_home() / "switchui-workflows.db")


def _resolve_db_path(db_path: Optional[str]) -> str:
    """Return db_path, falling back to WORKFLOW_DB_PATH env var, then the compiled default."""
    return db_path or os.environ.get("WORKFLOW_DB_PATH") or _DEFAULT_DB_PATH


def create_engine(
    db_path: Optional[str] = None,
    *,
    seed_bundled: bool = True,
    write_manifest: bool = True,
    crash_recovery: bool = True,
) -> "WorkflowEngine":  # noqa: F821
    """
    Open DB, ensure schema, wire all components, return WorkflowEngine.

    This is a synchronous factory — the engine's async methods are called
    later from FastAPI route handlers (Phase 3).
    """
    from engine.facade import WorkflowEngine

    path = _resolve_db_path(db_path)
    logger.info("wiring: opening DB at %s", path)

    # open_db is a context manager; for a long-lived engine we open manually
    if path == ":memory:":
        conn = sqlite3.connect(":memory:", check_same_thread=False)
    else:
        db_file = Path(path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_file), check_same_thread=False)

    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row

    ensure_schema(conn)

    run_store = RunStore(conn)
    def_store = DefinitionStore(conn)
    bus = EventBus(run_store=run_store)
    runner = WorkflowRunner(run_store, def_store, bus)
    manifest_writer = ManifestWriter(def_store)

    # Boot sequence
    boot: dict = {}

    if crash_recovery:
        boot["crashed_runs"] = mark_crashed_runs(run_store)

    if seed_bundled:
        seed_result = seed_defaults(def_store)
        boot["seed"] = seed_result

    if write_manifest:
        manifest_result = manifest_writer.write()
        boot["manifest"] = manifest_result

    engine = WorkflowEngine(
        conn=conn,
        run_store=run_store,
        def_store=def_store,
        bus=bus,
        runner=runner,
        manifest_writer=manifest_writer,
        boot=boot,
    )
    logger.info("wiring: engine ready (boot=%s)", boot)
    return engine


def dev_context(db_path: str = ":memory:") -> dict:
    """Return a minimal context dict for smoke-test use."""
    return {"db_path": db_path}
