"""
Tests for the run-view projector.

test_projector_golden: load golden-events.json, run through project_run(),
assert shape and field values are correct.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.projector.run_view import project_run, RunView

_FIXTURES = Path(__file__).parent / "fixtures" / "projector"
_GOLDEN_EVENTS = _FIXTURES / "golden-events.json"


def _load_golden_events():
    with open(_GOLDEN_EVENTS) as f:
        return json.load(f)


def test_projector_golden():
    """Load golden events, project, assert RunView matches expected shape."""
    events = _load_golden_events()
    view = project_run(events)

    assert view.run_id == "run-golden-1"
    assert view.status == "completed"
    assert view.event_count == 4

    # One node_run for 'greet'
    assert len(view.node_runs) == 1
    nr = view.node_runs[0]
    assert nr.dag_node_id == "greet"
    assert nr.node_type == "bash"
    assert nr.status == "completed"
    assert nr.started_at == 1700000001000
    assert nr.completed_at == 1700000001500
    assert nr.duration_ms == 500

    # to_dict round-trip
    d = view.to_dict()
    assert d["run_id"] == "run-golden-1"
    assert d["status"] == "completed"
    assert len(d["node_runs"]) == 1
    assert d["node_runs"][0]["dag_node_id"] == "greet"


def test_projector_empty():
    """Empty event list returns empty RunView."""
    view = project_run([])
    assert view.run_id == ""
    assert view.node_runs == []
    assert view.event_count == 0


def test_projector_workflow_failed():
    """workflow_failed event sets status and error."""
    events = [
        {
            "run_id": "run-fail-1",
            "event_type": "workflow_started",
            "node_run_id": None,
            "data": {"workflow_id": "wf"},
            "created_at": 1700000000000,
        },
        {
            "run_id": "run-fail-1",
            "event_type": "node_started",
            "node_run_id": "nr-a",
            "data": {"node_id": "step1", "node_type": "bash"},
            "created_at": 1700000001000,
        },
        {
            "run_id": "run-fail-1",
            "event_type": "node_failed",
            "node_run_id": "nr-a",
            "data": {"node_id": "step1", "error": "exit code 1"},
            "created_at": 1700000002000,
        },
        {
            "run_id": "run-fail-1",
            "event_type": "workflow_failed",
            "node_run_id": None,
            "data": {"error": "node step1 failed"},
            "created_at": 1700000002000,
        },
    ]
    view = project_run(events)
    assert view.status == "failed"
    assert view.error == "node step1 failed"
    assert view.node_runs[0].status == "failed"
    assert view.node_runs[0].error == "exit code 1"


def test_projector_cancelled():
    """workflow_cancelled sets status=cancelled."""
    events = [
        {
            "run_id": "run-c1",
            "event_type": "workflow_started",
            "node_run_id": None,
            "data": {},
            "created_at": 1700000000000,
        },
        {
            "run_id": "run-c1",
            "event_type": "workflow_cancelled",
            "node_run_id": None,
            "data": {},
            "created_at": 1700000001000,
        },
    ]
    view = project_run(events)
    assert view.status == "cancelled"


def test_projector_approval_pause():
    """approval_requested sets node status=paused with message."""
    events = [
        {
            "run_id": "run-ap1",
            "event_type": "workflow_started",
            "node_run_id": None,
            "data": {},
            "created_at": 1700000000000,
        },
        {
            "run_id": "run-ap1",
            "event_type": "node_started",
            "node_run_id": "nr-gate",
            "data": {"node_id": "gate", "node_type": "approval"},
            "created_at": 1700000001000,
        },
        {
            "run_id": "run-ap1",
            "event_type": "approval_requested",
            "node_run_id": "nr-gate",
            "data": {"node_id": "gate", "message": "Please approve"},
            "created_at": 1700000002000,
        },
    ]
    view = project_run(events)
    assert len(view.node_runs) == 1
    nr = view.node_runs[0]
    assert nr.status == "paused"
    assert nr.approval_message == "Please approve"


def test_projector_node_skipped():
    """node_skipped sets status=skipped with skip_reason."""
    events = [
        {
            "run_id": "run-skip1",
            "event_type": "workflow_started",
            "node_run_id": None,
            "data": {},
            "created_at": 1700000000000,
        },
        {
            "run_id": "run-skip1",
            "event_type": "node_skipped",
            "node_run_id": None,
            "data": {"node_id": "opt", "reason": "when_condition"},
            "created_at": 1700000001000,
        },
    ]
    view = project_run(events)
    assert view.node_runs[0].status == "skipped"
    assert view.node_runs[0].skip_reason == "when_condition"
