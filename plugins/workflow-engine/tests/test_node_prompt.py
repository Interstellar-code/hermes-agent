"""Tests for engine/nodes/prompt.py — uses a mock ctx.llm."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from engine.nodes.prompt import execute_prompt_node
from engine.core.dag_executor import DagRunContext, NodeExecutionResult
from engine.schemas.workflow_run import make_node_output
from engine.schemas.dag_node import validate_dag_node


def _parse_node(data):
    node, errors = validate_dag_node(data, 0)
    if errors:
        raise ValueError(f"Invalid node: {errors}")
    return node


def _make_mock_llm(response_text: str = "mocked response") -> MagicMock:
    """
    Mock matching the real PluginLlmFacade.complete() signature:

        def complete(
            self,
            messages: List[Dict[str, Any]],
            *,
            provider=None, model=None, temperature=None,
            max_tokens=None, timeout=None, agent_id=None,
            profile=None, purpose=None,
        ) -> PluginLlmCompleteResult  # .text
    """
    result = MagicMock()
    result.text = response_text
    llm = MagicMock()
    llm.complete.return_value = result
    return llm


def _make_ctx(llm=None):
    events = []
    ctx = DagRunContext(
        run_id="test-run",
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


@pytest.mark.asyncio
async def test_prompt_node_success():
    llm = _make_mock_llm("Analysis complete")
    ctx, events = _make_ctx(llm)
    node = _parse_node({"id": "analyze", "prompt": "Analyze this code"})
    result = await execute_prompt_node(node, {}, ctx, llm=llm)
    assert result.state == "completed"
    assert result.output == "Analysis complete"
    # Verify correct call shape: messages positional, purpose as kwarg
    llm.complete.assert_called_once()
    call_args = llm.complete.call_args
    messages = call_args[0][0]  # first positional arg
    assert isinstance(messages, list)
    assert messages[0]["role"] == "user"
    assert "Analyze this code" in messages[0]["content"]
    assert call_args[1]["purpose"].startswith("workflow-node:")


@pytest.mark.asyncio
async def test_prompt_node_output_substitution():
    """$nodeId.output refs are substituted into the prompt before calling llm."""
    llm = _make_mock_llm("done")
    ctx, _ = _make_ctx(llm)
    node_outputs = {"prev": make_node_output("completed", "RESULT_VALUE")}
    node = _parse_node({"id": "use_prev", "prompt": "Use this: $prev.output"})
    await execute_prompt_node(node, node_outputs, ctx, llm=llm)
    call_args = llm.complete.call_args
    messages = call_args[0][0]
    assert "RESULT_VALUE" in messages[0]["content"]


@pytest.mark.asyncio
async def test_prompt_node_no_llm_fails():
    ctx, events = _make_ctx()  # no llm
    node = _parse_node({"id": "p", "prompt": "hello"})
    result = await execute_prompt_node(node, {}, ctx, llm=None)
    assert result.state == "failed"
    assert "node_failed" in [e[0] for e in events]


@pytest.mark.asyncio
async def test_prompt_node_llm_exception():
    llm = MagicMock()
    llm.complete.side_effect = RuntimeError("provider error")
    ctx, events = _make_ctx(llm)
    node = _parse_node({"id": "p", "prompt": "hello"})
    result = await execute_prompt_node(node, {}, ctx, llm=llm)
    assert result.state == "failed"
    assert "provider error" in (result.error or "")
