"""Tests for subgraph expansion in execute_dag."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock
from typing import Optional, Tuple

from engine.core.dag_executor import DagRunContext, execute_dag
from engine.schemas.dag_node import validate_dag_node
from engine.schemas.workflow_run import make_node_output


def _parse_node(data):
    node, errors = validate_dag_node(data, 0)
    if errors:
        raise ValueError(f"Invalid node: {errors}")
    return node


CHILD_WORKFLOW_YAML = """\
id: inner-wf
name: Inner Workflow
description: A simple subgraph
kind: subgraph

nodes:
  - id: inner-step
    bash: echo "inner $INPUTS.greeting"
"""


def _make_ctx(subgraph_store=None):
    events = []

    def get_subgraph_yaml(ref: str) -> Optional[Tuple[str, str]]:
        if subgraph_store and ref in subgraph_store:
            return subgraph_store[ref]
        return None

    return DagRunContext(
        run_id="subgraph-run",
        emit_event=lambda t, p: events.append((t, p)),
        get_run_status=AsyncMock(return_value="running"),
        pause_run=AsyncMock(),
        cancel_run=AsyncMock(),
        send_message=AsyncMock(),
        get_subgraph_yaml=get_subgraph_yaml,
    ), events


@pytest.mark.asyncio
async def test_subgraph_expansion_inline():
    """Subgraph reference expands inline, child events appear with namespaced ids."""
    store = {"inner-wf": (CHILD_WORKFLOW_YAML, "subgraph")}
    ctx, events = _make_ctx(store)

    nodes = [
        _parse_node({
            "id": "call-sub",
            "subgraph": {
                "ref": "inner-wf",
                "inputs": {"greeting": "hello"},
            },
        })
    ]

    outputs = await execute_dag(nodes, ctx)

    # The child node should have been executed with namespaced id
    child_id = "call-sub.inner-step"
    assert child_id in outputs
    assert outputs[child_id].state == "completed"
    assert "inner" in outputs[child_id].output

    # subgraph_started event emitted
    subgraph_events = [e for e in events if e[0] == "subgraph_started"]
    assert len(subgraph_events) == 1
    assert subgraph_events[0][1]["node_id"] == "call-sub"


@pytest.mark.asyncio
async def test_subgraph_when_skip():
    """Subgraph with subgraph.when=false is skipped at expansion time, node_skipped emitted.
    Fix HIGH-4: the condition is read from node.subgraph.when, not node.when.
    """
    store = {"inner-wf": (CHILD_WORKFLOW_YAML, "subgraph")}
    ctx, events = _make_ctx(store)

    nodes = [
        _parse_node({
            "id": "trigger",
            "bash": "echo trigger",
        }),
        _parse_node({
            "id": "skipped-sub",
            "depends_on": ["trigger"],
            "subgraph": {
                "ref": "inner-wf",
                "when": "false",   # subgraph.when, not top-level when
            },
        }),
    ]

    outputs = await execute_dag(nodes, ctx)

    skipped_events = [e for e in events if e[0] == "node_skipped"]
    assert any(e[1]["node_id"] == "skipped-sub" for e in skipped_events)


@pytest.mark.asyncio
async def test_subgraph_not_found_raises():
    """Missing subgraph ref → DAG raises (Fix HIGH-11: TS parity — re-raise, not silent fail)."""
    ctx, events = _make_ctx(subgraph_store={})

    nodes = [
        _parse_node({
            "id": "missing-sub",
            "subgraph": {"ref": "does-not-exist"},
        })
    ]

    with pytest.raises(ValueError, match="not found"):
        await execute_dag(nodes, ctx)
