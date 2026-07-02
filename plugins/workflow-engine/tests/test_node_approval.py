"""Tests for engine/nodes/approval.py."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from engine.nodes.approval import execute_approval_node
from engine.core.dag_executor import DagRunContext
from engine.schemas.dag_node import validate_dag_node
from engine.schemas.workflow_run import make_node_output


def _parse_node(data):
    node, errors = validate_dag_node(data, 0)
    if errors:
        raise ValueError(f"Invalid node: {errors}")
    return node


def _make_ctx():
    events = []
    paused = []

    async def pause_run(meta):
        paused.append(meta)

    ctx = DagRunContext(
        run_id="run-approval",
        emit_event=lambda t, p: events.append((t, p)),
        get_run_status=AsyncMock(return_value="paused"),
        pause_run=pause_run,
        cancel_run=AsyncMock(),
        send_message=AsyncMock(),
        get_subgraph_yaml=lambda ref: None,
    )
    return ctx, events, paused


@pytest.mark.asyncio
async def test_approval_node_pauses_run():
    ctx, events, paused = _make_ctx()
    node = _parse_node({
        "id": "review-gate",
        "approval": {"message": "Please review the PR diff"},
    })
    result = await execute_approval_node(node, {}, ctx)

    # Node returns completed (between-layer check sees paused and breaks)
    assert result.state == "completed"

    # pause_run was called with correct metadata
    assert len(paused) == 1
    assert paused[0]["type"] == "approval"
    assert paused[0]["node_id"] == "review-gate"
    assert "Please review the PR diff" in paused[0]["message"]

    # approval_requested event emitted
    event_types = [e[0] for e in events]
    assert "approval_requested" in event_types
    assert "node_started" in event_types


@pytest.mark.asyncio
async def test_approval_node_message_substitution():
    """$nodeId.output refs in approval message are substituted."""
    ctx, events, paused = _make_ctx()
    node_outputs = {"analyze": make_node_output("completed", "DIFF_SUMMARY")}
    node = _parse_node({
        "id": "gate",
        "approval": {"message": "Review: $analyze.output"},
    })
    await execute_approval_node(node, node_outputs, ctx)

    assert len(paused) == 1
    assert "DIFF_SUMMARY" in paused[0]["message"]
