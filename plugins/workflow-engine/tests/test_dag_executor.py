"""
Tests for dag_executor.py:
- build_topological_layers (Kahn's algorithm)
- check_trigger_rule
- execute_dag: linear, parallel fanout, conditional skip, subgraph, approval
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.core.dag_executor import (
    DagRunContext,
    NodeExecutionResult,
    build_topological_layers,
    check_trigger_rule,
    execute_dag,
)
from engine.schemas.dag_node import (
    BashNode,
    DagNodeBase,
    validate_dag_node,
)
from engine.schemas.workflow_run import make_node_output


def _parse_node(data):
    """Parse a node dict, raising on validation errors."""
    node, errors = validate_dag_node(data, 0)
    if errors:
        raise ValueError(f"Invalid node: {errors}")
    return node


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_bash_node(node_id: str, bash: str = "echo ok", depends_on: Optional[List[str]] = None, when: Optional[str] = None) -> Any:
    data: Dict[str, Any] = {"id": node_id, "bash": bash}
    if depends_on:
        data["depends_on"] = depends_on
    if when is not None:
        data["when"] = when
    return _parse_node(data)


def _make_ctx(events: Optional[List] = None, run_status: str = "running") -> DagRunContext:
    """Build a minimal DagRunContext for testing."""
    if events is None:
        events = []

    def emit(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    async def get_status() -> Optional[str]:
        return run_status

    async def pause_run(meta: dict) -> None:
        pass

    async def cancel_run() -> None:
        pass

    async def send_message(msg: str) -> None:
        pass

    def get_subgraph_yaml(ref: str) -> Optional[Tuple[str, str]]:
        return None

    return DagRunContext(
        run_id="test-run-id",
        emit_event=emit,
        get_run_status=get_status,
        pause_run=pause_run,
        cancel_run=cancel_run,
        send_message=send_message,
        get_subgraph_yaml=get_subgraph_yaml,
    )


# ── Topological Layers ────────────────────────────────────────────────────────

class TestBuildTopologicalLayers:
    def test_no_deps_single_layer(self):
        nodes = [_make_bash_node("a"), _make_bash_node("b"), _make_bash_node("c")]
        layers = build_topological_layers(nodes)
        assert len(layers) == 1
        assert {n.id for n in layers[0]} == {"a", "b", "c"}

    def test_linear_chain(self):
        nodes = [
            _make_bash_node("a"),
            _make_bash_node("b", depends_on=["a"]),
            _make_bash_node("c", depends_on=["b"]),
        ]
        layers = build_topological_layers(nodes)
        assert len(layers) == 3
        assert [n.id for layer in layers for n in layer] == ["a", "b", "c"]

    def test_fanout(self):
        nodes = [
            _make_bash_node("root"),
            _make_bash_node("child1", depends_on=["root"]),
            _make_bash_node("child2", depends_on=["root"]),
            _make_bash_node("child3", depends_on=["root"]),
        ]
        layers = build_topological_layers(nodes)
        assert len(layers) == 2
        assert layers[0][0].id == "root"
        assert {n.id for n in layers[1]} == {"child1", "child2", "child3"}

    def test_diamond(self):
        nodes = [
            _make_bash_node("a"),
            _make_bash_node("b", depends_on=["a"]),
            _make_bash_node("c", depends_on=["a"]),
            _make_bash_node("d", depends_on=["b", "c"]),
        ]
        layers = build_topological_layers(nodes)
        assert len(layers) == 3
        assert layers[0][0].id == "a"
        assert {n.id for n in layers[1]} == {"b", "c"}
        assert layers[2][0].id == "d"

    def test_cycle_raises(self):
        nodes = [
            _make_bash_node("a", depends_on=["b"]),
            _make_bash_node("b", depends_on=["a"]),
        ]
        with pytest.raises(ValueError, match="Cycle detected"):
            build_topological_layers(nodes)


# ── Trigger Rule ──────────────────────────────────────────────────────────────

class TestCheckTriggerRule:
    def _output(self, state: str):
        return make_node_output(state, "")

    def test_no_deps_always_run(self):
        node = _make_bash_node("x")
        assert check_trigger_rule(node, {}) == "run"

    def test_all_success_all_complete(self):
        node = _make_bash_node("x", depends_on=["a", "b"])
        outputs = {"a": self._output("completed"), "b": self._output("completed")}
        assert check_trigger_rule(node, outputs) == "run"

    def test_all_success_one_failed(self):
        node = _make_bash_node("x", depends_on=["a", "b"])
        outputs = {"a": self._output("completed"), "b": self._output("failed")}
        assert check_trigger_rule(node, outputs) == "skip"

    def test_always_rule(self):
        data = {"id": "x", "bash": "echo", "depends_on": ["a"], "trigger_rule": "all_done"}
        node = _parse_node(data)
        outputs = {"a": self._output("failed")}
        assert check_trigger_rule(node, outputs) == "run"

    def test_one_success_rule(self):
        data = {"id": "x", "bash": "echo", "depends_on": ["a", "b"], "trigger_rule": "one_success"}
        node = _parse_node(data)
        outputs = {"a": self._output("failed"), "b": self._output("completed")}
        assert check_trigger_rule(node, outputs) == "run"


# ── execute_dag: linear ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_dag_linear():
    """4 nodes in linear chain — all complete in order."""
    nodes = [
        _make_bash_node("step1", bash="echo step1"),
        _make_bash_node("step2", bash="echo step2", depends_on=["step1"]),
        _make_bash_node("step3", bash="echo step3", depends_on=["step2"]),
        _make_bash_node("step4", bash="echo step4", depends_on=["step3"]),
    ]
    events: List = []
    ctx = _make_ctx(events)

    outputs = await execute_dag(nodes, ctx)

    assert outputs["step1"].state == "completed"
    assert outputs["step1"].output == "step1"
    assert outputs["step4"].state == "completed"
    assert outputs["step4"].output == "step4"

    completed_events = [e for e in events if e[0] == "node_completed"]
    assert len(completed_events) == 4


# ── execute_dag: parallel fanout ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_dag_parallel_fanout():
    """3 children of fan_out start within the same asyncio gather call."""
    start_times: Dict[str, float] = {}

    async def timed_bash(node_id: str, delay: float = 0.01) -> NodeExecutionResult:
        start_times[node_id] = time.monotonic()
        await asyncio.sleep(delay)
        return NodeExecutionResult(state="completed", output=node_id)

    # Patch dispatch_node to record start times
    import engine.core.node_dispatcher as dispatcher_mod
    original_dispatch = dispatcher_mod.dispatch_node

    async def mock_dispatch(node, node_outputs, ctx):
        return await timed_bash(node.id, delay=0.02)

    dispatcher_mod.dispatch_node = mock_dispatch
    try:
        nodes = [
            _make_bash_node("fan_out"),
            _make_bash_node("child_a", depends_on=["fan_out"]),
            _make_bash_node("child_b", depends_on=["fan_out"]),
            _make_bash_node("child_c", depends_on=["fan_out"]),
        ]
        ctx = _make_ctx()
        outputs = await execute_dag(nodes, ctx)
    finally:
        dispatcher_mod.dispatch_node = original_dispatch

    # All children should have started (fan_out runs first)
    assert "child_a" in start_times
    assert "child_b" in start_times
    assert "child_c" in start_times

    # Children start within 50ms of each other (they're in the same gather)
    child_starts = [start_times[c] for c in ("child_a", "child_b", "child_c")]
    spread_ms = (max(child_starts) - min(child_starts)) * 1000
    assert spread_ms < 50, f"Children started {spread_ms:.1f}ms apart (expected <50ms)"

    assert outputs["child_a"].state == "completed"
    assert outputs["child_b"].state == "completed"
    assert outputs["child_c"].state == "completed"


# ── execute_dag: conditional skip ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_dag_conditional_skip():
    """when: condition skips the false branch."""
    nodes = [
        _make_bash_node("classify", bash="echo BUG"),
        _make_bash_node("handle_bug", bash="echo handling", depends_on=["classify"],
                        when="$classify.output == 'BUG'"),
        _make_bash_node("handle_feature", bash="echo handling", depends_on=["classify"],
                        when="$classify.output == 'FEATURE'"),
    ]
    ctx = _make_ctx()
    outputs = await execute_dag(nodes, ctx)

    assert outputs["classify"].state == "completed"
    assert outputs["handle_bug"].state == "completed"
    assert outputs["handle_feature"].state == "skipped"


# ── execute_dag: approval node ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_dag_approval_pauses():
    """Approval node sets run status = paused; between-layer check breaks the loop."""
    paused_calls: List = []

    async def pause_run(meta: dict) -> None:
        paused_calls.append(meta)

    status_sequence = ["running", "paused"]
    call_count = [0]

    async def get_status() -> Optional[str]:
        idx = min(call_count[0], len(status_sequence) - 1)
        call_count[0] += 1
        return status_sequence[idx]

    async def send_message(msg: str) -> None:
        pass

    ctx = DagRunContext(
        run_id="approval-test-run",
        emit_event=lambda *a: None,
        get_run_status=get_status,
        pause_run=pause_run,
        cancel_run=AsyncMock(),
        send_message=send_message,
        get_subgraph_yaml=lambda ref: None,
    )

    approval_data = {
        "id": "gate",
        "approval": {"message": "Please review and approve"},
    }
    gate_node = _parse_node(approval_data)
    nodes = [gate_node]

    outputs = await execute_dag(nodes, ctx)

    # Pause was called
    assert len(paused_calls) == 1
    assert paused_calls[0]["type"] == "approval"
    assert paused_calls[0]["node_id"] == "gate"

    # Node completed (approval node returns completed so the status-check breaks)
    assert outputs["gate"].state == "completed"


# ── execute_dag: cycle detection ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_dag_cycle_raises():
    nodes = [
        _make_bash_node("a", depends_on=["b"]),
        _make_bash_node("b", depends_on=["a"]),
    ]
    ctx = _make_ctx()
    with pytest.raises(ValueError, match="Cycle detected"):
        await execute_dag(nodes, ctx)


# ── execute_dag: prior_completed (resume) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_dag_resume_skips_completed():
    """Nodes in prior_completed are skipped and their output is preserved."""
    nodes = [
        _make_bash_node("step1", bash="echo NEW"),
        _make_bash_node("step2", bash="echo step2", depends_on=["step1"]),
    ]
    ctx = _make_ctx()
    ctx.prior_completed = {"step1": "PRIOR_OUTPUT"}

    outputs = await execute_dag(nodes, ctx)

    # step1 was skipped (prior_completed), output preserved
    assert outputs["step1"].state == "completed"
    assert outputs["step1"].output == "PRIOR_OUTPUT"
    # step2 ran normally
    assert outputs["step2"].state == "completed"
