"""Tests for engine/nodes/loop.py."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from engine.nodes.loop import execute_loop_node
from engine.core.dag_executor import DagRunContext
from engine.schemas.dag_node import validate_dag_node
from engine.schemas.workflow_run import make_node_output


def _parse_node(data):
    node, errors = validate_dag_node(data, 0)
    if errors:
        raise ValueError(f"Invalid node: {errors}")
    return node


def _make_ctx(llm=None):
    events = []
    ctx = DagRunContext(
        run_id="loop-run",
        emit_event=lambda t, p: events.append((t, p)),
        get_run_status=AsyncMock(return_value="running"),
        pause_run=AsyncMock(),
        cancel_run=AsyncMock(),
        send_message=AsyncMock(),
        get_subgraph_yaml=lambda ref: None,
    )
    if llm is not None:
        ctx.llm = llm
    return ctx, events


def _make_llm(responses):
    """LLM that returns successive responses."""
    call_count = [0]
    llm = MagicMock()

    def complete(messages, **kwargs):
        idx = min(call_count[0], len(responses) - 1)
        call_count[0] += 1
        r = MagicMock()
        r.text = responses[idx]
        return r

    llm.complete.side_effect = complete
    return llm


@pytest.mark.asyncio
async def test_loop_over_list_no_llm():
    """Loop over a list without llm — falls back to returning the prompt string."""
    node = _parse_node({
        "id": "iterate",
        "loop": {
            "over": ["a", "b", "c"],
            "prompt": "item=$LOOP_ITEM",
            "max_iterations": 3,
        },
    })
    ctx, events = _make_ctx()
    result = await execute_loop_node(node, {}, ctx)
    assert result.state == "completed"
    # Last iteration had item=c
    assert "c" in result.output

    iter_events = [e for e in events if e[0] == "loop_iteration_completed"]
    assert len(iter_events) == 3


@pytest.mark.asyncio
async def test_loop_over_list_with_llm():
    """Loop over list — each item gets an LLM call."""
    llm = _make_llm(["response_a", "response_b", "response_c"])
    node = _parse_node({
        "id": "iterate",
        "loop": {
            "over": ["item_a", "item_b", "item_c"],
            "prompt": "Process: $LOOP_ITEM",
            "max_iterations": 3,
        },
    })
    ctx, events = _make_ctx(llm=llm)
    result = await execute_loop_node(node, {}, ctx, )
    assert result.state == "completed"
    assert result.output == "response_c"
    assert llm.complete.call_count == 3


@pytest.mark.asyncio
async def test_loop_max_iterations():
    """Loop with max_iterations and no over: uses range."""
    node = _parse_node({
        "id": "fixed-loop",
        "loop": {
            "max_iterations": 2,
            "prompt": "iter $LOOP_INDEX",
        },
    })
    ctx, events = _make_ctx()
    result = await execute_loop_node(node, {}, ctx)
    assert result.state == "completed"
    iter_events = [e for e in events if e[0] == "loop_iteration_completed"]
    assert len(iter_events) == 2


@pytest.mark.asyncio
async def test_loop_empty_over_list():
    node = _parse_node({
        "id": "empty-loop",
        "loop": {
            "over": [],
            "prompt": "p",
            "max_iterations": 1,
        },
    })
    ctx, events = _make_ctx()
    result = await execute_loop_node(node, {}, ctx)
    assert result.state == "completed"
    assert result.output == ""
