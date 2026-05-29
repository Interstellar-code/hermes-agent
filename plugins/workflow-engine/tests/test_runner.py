"""
Tests for WorkflowRunner and RunStore.

Acceptance gates:
- test_full_cycle: start hello-world run, completes, DB rows correct
- test_cancel_mid_flight: cancel during sleep node, status=cancelled
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from engine.db.migrate import ensure_schema
from engine.store.run_store import RunStore
from engine.store.definition_store import DefinitionStore
from engine.emitter.bus import EventBus
from engine.runtime.runner import WorkflowRunner


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _seed_workflow(def_store: DefinitionStore, wf_id: str, yaml_text: str) -> None:
    conn = def_store._conn
    now = int(time.time() * 1000)
    conn.execute(
        """INSERT INTO workflow_definitions
             (id, name, description, source, yaml, checksum, created_at, updated_at, kind)
           VALUES (?, ?, ?, 'bundled', ?, 'test', ?, ?, 'workflow')""",
        (wf_id, wf_id, wf_id, yaml_text, now, now),
    )
    conn.commit()


HELLO_WORLD_YAML = """
name: hello-world
description: Hello World workflow
nodes:
  - id: greet
    bash: echo "hello world"
"""

SLEEP_YAML = """
name: sleep-workflow
description: Sleeps for a long time
nodes:
  - id: sleeper
    bash: sleep 60
"""

APPROVAL_YAML = """
name: approval-workflow
description: Workflow with approval gate
nodes:
  - id: before
    bash: echo "before approval"
  - id: gate
    approval:
      message: "Please approve to continue"
    depends_on: [before]
  - id: after
    bash: echo "after approval"
    depends_on: [gate]
"""

FAIL_YAML = """
name: fail-workflow
description: Workflow with a failing node
nodes:
  - id: broken
    bash: exit 1
"""


@pytest.fixture()
def runner_env():
    conn = _make_conn()
    run_store = RunStore(conn)
    def_store = DefinitionStore(conn)
    bus = EventBus(run_store=run_store)
    runner = WorkflowRunner(run_store, def_store, bus)
    return {"conn": conn, "run_store": run_store, "def_store": def_store, "bus": bus, "runner": runner}


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_cycle(runner_env):
    """Start hello-world run, wait for completion, assert DB rows correct."""
    env = runner_env
    _seed_workflow(env["def_store"], "hello-world", HELLO_WORLD_YAML)

    run = await env["runner"].start(
        "hello-world",
        {},
        {"kind": "manual", "conversation_id": "conv-1", "working_path": "/tmp"},
    )
    assert run["status"] == "running"
    run_id = run["id"]

    # Wait for background task to complete (max 5s)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        refreshed = env["run_store"].get_workflow_run(run_id)
        if refreshed and refreshed["status"] in ("completed", "failed", "cancelled"):
            break

    final = env["run_store"].get_workflow_run(run_id)
    assert final is not None
    assert final["status"] == "completed", f"Expected completed, got {final['status']} error={final.get('error')}"

    # DB rows: at least 1 node_run
    node_runs = env["run_store"].list_node_runs(run_id)
    assert len(node_runs) >= 1

    # DB rows: events
    events = env["run_store"].list_events(run_id, limit=100)
    event_types = {e["event_type"] for e in events}
    assert "workflow_started" in event_types
    assert "workflow_completed" in event_types


@pytest.mark.asyncio
async def test_cancel_mid_flight(runner_env):
    """Cancel a long-running bash node, assert run status=cancelled."""
    env = runner_env
    _seed_workflow(env["def_store"], "sleep-workflow", SLEEP_YAML)

    run = await env["runner"].start(
        "sleep-workflow",
        {},
        {"kind": "manual", "conversation_id": "conv-cancel", "working_path": "/tmp"},
    )
    run_id = run["id"]

    # Give it a moment to actually start
    await asyncio.sleep(0.15)

    # Cancel
    await env["runner"].cancel(run_id)

    # Wait briefly for cancel to propagate
    await asyncio.sleep(0.2)

    final = env["run_store"].get_workflow_run(run_id)
    assert final is not None
    assert final["status"] == "cancelled", f"Expected cancelled, got {final['status']}"


def test_build_ctx_includes_injected_llm(runner_env):
    """Prompt/command nodes need ctx.llm wired from the plugin host."""
    env = runner_env
    llm = MagicMock()
    env["runner"].set_llm(llm)

    ctx = env["runner"]._build_ctx("run-1", "/tmp")

    assert ctx.llm is llm


@pytest.mark.asyncio
async def test_failed_node_marks_run_failed(runner_env):
    """A node_failed result should finalize the workflow as failed, not completed."""
    env = runner_env
    _seed_workflow(env["def_store"], "fail-workflow", FAIL_YAML)

    run = await env["runner"].start(
        "fail-workflow",
        {},
        {"kind": "manual", "conversation_id": "conv-fail", "working_path": "/tmp"},
    )
    run_id = run["id"]

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        refreshed = env["run_store"].get_workflow_run(run_id)
        if refreshed and refreshed["status"] in ("completed", "failed", "cancelled"):
            break

    final = env["run_store"].get_workflow_run(run_id)
    assert final is not None
    assert final["status"] == "failed", (
        f"Expected failed, got {final['status']} error={final.get('error')}"
    )
    assert "broken" in (final["error"] or "") or "exit" in (final["error"] or "").lower()

    events = env["run_store"].list_events(run_id, limit=100)
    event_types = {e["event_type"] for e in events}
    assert "node_failed" in event_types
    assert "workflow_failed" in event_types
    assert "workflow_completed" not in event_types
