"""Tests for engine/nodes/bash.py."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from engine.nodes.bash import execute_bash_node
from engine.core.dag_executor import DagRunContext, NodeExecutionResult
from engine.schemas.workflow_run import make_node_output
from engine.schemas.dag_node import validate_dag_node


def _parse_node(data):
    node, errors = validate_dag_node(data, 0)
    if errors:
        raise ValueError(f"Invalid node: {errors}")
    return node


def _make_ctx():
    events = []
    return DagRunContext(
        run_id="test-run",
        emit_event=lambda t, p: events.append((t, p)),
        get_run_status=AsyncMock(return_value="running"),
        pause_run=AsyncMock(),
        cancel_run=AsyncMock(),
        send_message=AsyncMock(),
        get_subgraph_yaml=lambda ref: None,
    ), events


@pytest.mark.asyncio
async def test_bash_node_success():
    node = _parse_node({"id": "greet", "bash": "echo hello"})
    ctx, events = _make_ctx()
    result = await execute_bash_node(node, {}, ctx)
    assert result.state == "completed"
    assert result.output == "hello"
    types = [e[0] for e in events]
    assert "node_started" in types
    assert "node_completed" in types


@pytest.mark.asyncio
async def test_bash_node_failure():
    node = _parse_node({"id": "fail", "bash": "exit 1"})
    ctx, events = _make_ctx()
    result = await execute_bash_node(node, {}, ctx)
    assert result.state == "failed"
    assert result.error is not None
    assert "node_failed" in [e[0] for e in events]


@pytest.mark.asyncio
async def test_bash_node_output_substitution():
    node_outputs = {"prev": make_node_output("completed", "world")}
    node = _parse_node({"id": "use_prev", "bash": "echo $prev.output"})
    ctx, _ = _make_ctx()
    result = await execute_bash_node(node, node_outputs, ctx)
    assert result.state == "completed"
    assert "world" in result.output


@pytest.mark.asyncio
async def test_bash_node_timeout():
    node = _parse_node({"id": "slow", "bash": "sleep 100", "timeout": 0.05})
    ctx, events = _make_ctx()
    result = await execute_bash_node(node, {}, ctx)
    assert result.state == "failed"
    assert "timed out" in (result.error or "").lower()
