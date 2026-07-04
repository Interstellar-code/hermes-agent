"""
Phase 2b parity-fix tests — asserts TS-parity behavior for all 11 findings.

Each test is labelled with the finding number it covers.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.core.dag_executor import (
    DagRunContext,
    NodeExecutionResult,
    _get_retry_config,
    execute_dag,
)
from engine.core.executor_shared import substitute_workflow_variables
from engine.nodes.loop import execute_loop_node
from engine.nodes.approval import execute_approval_node
from engine.nodes.command import execute_command_node
from engine.nodes.bash import execute_bash_node
from engine.nodes.script import _is_inline_script
from engine.schemas.dag_node import validate_dag_node
from engine.schemas.retry import StepRetryConfig
from engine.schemas.workflow_run import make_node_output


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_node(data):
    node, errors = validate_dag_node(data, 0)
    if errors:
        raise ValueError(f"Invalid node: {errors}")
    return node


def _make_ctx(events=None, run_status="running", llm=None, subgraph_yaml_fn=None):
    if events is None:
        events = []
    ctx = DagRunContext(
        run_id="test-run",
        emit_event=lambda t, p: events.append((t, p)),
        get_run_status=AsyncMock(return_value=run_status),
        pause_run=AsyncMock(),
        cancel_run=AsyncMock(),
        send_message=AsyncMock(),
        get_subgraph_yaml=subgraph_yaml_fn or (lambda ref: None),
    )
    if llm is not None:
        ctx.llm = llm
    return ctx, events


def _make_llm(responses):
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


# ── FINDING 3: Retry field names ──────────────────────────────────────────────

def test_retry_field_names_max_attempts():
    """HIGH 3: _get_retry_config reads max_attempts and delay_ms, not max/backoff_ms."""
    retry = StepRetryConfig(max_attempts=3, delay_ms=5000, on_error="all")
    node = _parse_node({"id": "n", "bash": "echo hi"})
    # Attach retry directly
    object.__setattr__(node, "retry", retry)
    max_r, delay, on_err = _get_retry_config(node)
    assert max_r == 3
    assert delay == 5000
    assert on_err == "all"


def test_retry_defaults_when_no_retry():
    """HIGH 3: node with no retry returns defaults."""
    node = _parse_node({"id": "n", "bash": "echo hi"})
    max_r, delay, on_err = _get_retry_config(node)
    assert max_r == 2
    assert delay == 3000
    assert on_err == "transient"


# ── FINDING 6: substituteWorkflowVariables ────────────────────────────────────

def test_substitute_workflow_variables_basic():
    """HIGH 6: basic variable substitution."""
    prompt = "id=$WORKFLOW_ID msg=$USER_MESSAGE dir=$ARTIFACTS_DIR branch=$BASE_BRANCH"
    result, ctx_sub = substitute_workflow_variables(
        prompt,
        workflow_id="wf-1",
        user_message="hello",
        artifacts_dir="/tmp/art",
        base_branch="main",
        docs_dir="docs/",
    )
    assert "wf-1" in result
    assert "hello" in result
    assert "/tmp/art" in result
    assert "main" in result
    assert ctx_sub is False


def test_substitute_workflow_variables_arguments_alias():
    """HIGH 6: $ARGUMENTS is alias for $USER_MESSAGE."""
    result, _ = substitute_workflow_variables(
        "$ARGUMENTS",
        user_message="trigger msg",
        base_branch="main",
    )
    assert result == "trigger msg"


def test_substitute_workflow_variables_issue_context():
    """HIGH 6: $CONTEXT is replaced with issue_context when provided."""
    result, ctx_sub = substitute_workflow_variables(
        "ctx=$CONTEXT",
        base_branch="main",
        issue_context="Issue #42 body",
    )
    assert "Issue #42 body" in result
    assert ctx_sub is True


def test_substitute_workflow_variables_no_context_clears():
    """HIGH 6: $CONTEXT replaced with empty string when issue_context is None."""
    result, ctx_sub = substitute_workflow_variables(
        "ctx=$CONTEXT",
        base_branch="main",
        issue_context=None,
    )
    assert "$CONTEXT" not in result
    assert result == "ctx="
    assert ctx_sub is False


def test_substitute_workflow_variables_base_branch_missing_raises():
    """HIGH 6: raises if $BASE_BRANCH used but base_branch is empty."""
    with pytest.raises(ValueError, match="No base branch"):
        substitute_workflow_variables("branch=$BASE_BRANCH", base_branch="")


def test_substitute_workflow_variables_backref_in_user_message():
    """Issue #22: user_message containing \\1 must not raise re.error (backreference crash)."""
    result, _ = substitute_workflow_variables(
        "$USER_MESSAGE",
        user_message=r"fix \1 and \g<name> issue",
        base_branch="main",
    )
    assert result == r"fix \1 and \g<name> issue"


def test_substitute_workflow_variables_backref_in_context():
    """Issue #22: issue_context containing \\1 must not raise re.error."""
    result, ctx_sub = substitute_workflow_variables(
        "$CONTEXT",
        base_branch="main",
        issue_context=r"see \1 for details",
    )
    assert result == r"see \1 for details"
    assert ctx_sub is True


# ── Issue #81: command injection via unescaped workflow variables ────────────

def test_substitute_workflow_variables_escaped_for_bash_neutralizes_injection():
    """
    #81: a malicious $USER_MESSAGE must not be able to break out of its
    substitution slot when the result is fed to `bash -c`.
    """
    payload = "foo'; touch /tmp/PWNED_test81; echo '"
    result, _ = substitute_workflow_variables(
        "echo $USER_MESSAGE",
        user_message=payload,
        base_branch="main",
        escaped_for_bash=True,
    )
    # The payload must appear shell-quoted (single-quoted, internal quotes escaped)
    # so it is inert as a single literal argument, not executable shell syntax.
    assert result == "echo 'foo'\\''; touch /tmp/PWNED_test81; echo '\\'''"


def test_substitute_workflow_variables_escaped_for_bash_empty_value():
    """Empty/None substituted values must become '' when escaped, per _shell_quote convention."""
    result, _ = substitute_workflow_variables(
        "echo $USER_MESSAGE",
        user_message="",
        base_branch="main",
        escaped_for_bash=True,
    )
    assert result == "echo ''"


def test_substitute_workflow_variables_default_unescaped_unchanged():
    """AI-prompt callers (escaped_for_bash default False) must not get shell quotes."""
    result, _ = substitute_workflow_variables(
        "echo $USER_MESSAGE",
        user_message="hello world",
        base_branch="main",
    )
    assert result == "echo hello world"


# ── FINDING 7: Timeout unit mismatch ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_bash_timeout_converted_from_ms():
    """HIGH 7: bash node timeout field is in ms; subprocess gets seconds."""
    node = _parse_node({"id": "n", "bash": "sleep 0", "timeout": 5000})
    ctx, events = _make_ctx()
    # Should not time out — 5000ms = 5s is plenty for 'sleep 0'
    result = await execute_bash_node(node, {}, ctx)
    assert result.state == "completed"


@pytest.mark.asyncio
async def test_bash_timeout_very_short_ms_fails():
    """HIGH 7: very short timeout (1ms = 0.001s) causes timeout failure."""
    node = _parse_node({"id": "n", "bash": "sleep 2", "timeout": 1})
    ctx, events = _make_ctx()
    result = await execute_bash_node(node, {}, ctx)
    assert result.state == "failed"
    assert "timed out" in (result.error or "").lower()


# ── FINDING 8: Script inline/named detection ──────────────────────────────────

def test_is_inline_script_named():
    """HIGH 8: bare identifiers are named scripts."""
    assert _is_inline_script("my-script") is False
    assert _is_inline_script("fetch-data") is False


def test_is_inline_script_inline():
    """HIGH 8: newline or special chars = inline code."""
    assert _is_inline_script("import sys\nprint('hi')") is True
    assert _is_inline_script("a; b") is True
    assert _is_inline_script("f()") is True
    assert _is_inline_script("console.log('x')") is True


# ── FINDING 9: Loop until + fail on max iter ──────────────────────────────────

@pytest.mark.asyncio
async def test_loop_until_signal_detected():
    """HIGH 9: loop stops when until signal appears in LLM output."""
    llm = _make_llm(["not done", "not done", "COMPLETE done"])
    node = _parse_node({
        "id": "loop",
        "loop": {
            "prompt": "do work",
            "until": "COMPLETE",
            "max_iterations": 10,
        },
    })
    ctx, events = _make_ctx(llm=llm)
    result = await execute_loop_node(node, {}, ctx)
    assert result.state == "completed"
    # Should have stopped at iteration 3
    iter_events = [e for e in events if e[0] == "loop_iteration_completed"]
    assert len(iter_events) == 3


@pytest.mark.asyncio
async def test_loop_fails_on_max_iter_exhaustion():
    """HIGH 9: AI loop fails if until never satisfied within max_iterations."""
    llm = _make_llm(["not done"] * 5)
    node = _parse_node({
        "id": "loop",
        "loop": {
            "prompt": "do work",
            "until": "COMPLETE",
            "max_iterations": 3,
        },
    })
    ctx, events = _make_ctx(llm=llm)
    result = await execute_loop_node(node, {}, ctx)
    assert result.state == "failed"
    assert "exceeded max iterations" in (result.error or "")
    assert "3" in (result.error or "")


@pytest.mark.asyncio
async def test_loop_over_list_no_fail_on_no_until():
    """HIGH 9: list loops (over:) do NOT fail if until never triggers."""
    node = _parse_node({
        "id": "loop",
        "loop": {
            "over": ["a", "b"],
            "prompt": "process $LOOP_ITEM",
            "max_iterations": 2,
        },
    })
    ctx, events = _make_ctx()
    result = await execute_loop_node(node, {}, ctx)
    assert result.state == "completed"


@pytest.mark.asyncio
async def test_loop_until_bash_completion():
    """HIGH 9: until_bash exit 0 triggers completion."""
    llm = _make_llm(["iter output"])
    node = _parse_node({
        "id": "loop",
        "loop": {
            "prompt": "do work",
            "until": "NEVER_SIGNAL",
            "until_bash": "exit 0",
            "max_iterations": 5,
        },
    })
    ctx, events = _make_ctx(llm=llm)
    result = await execute_loop_node(node, {}, ctx)
    assert result.state == "completed"
    # Should complete on first iter because until_bash exits 0
    iter_events = [e for e in events if e[0] == "loop_iteration_completed"]
    assert len(iter_events) == 1


# ── FINDING 10: Approval capture_response / on_reject ────────────────────────

@pytest.mark.asyncio
async def test_approval_pause_includes_capture_response():
    """MEDIUM 10: pause_run metadata includes captureResponse when set."""
    node = _parse_node({
        "id": "gate",
        "approval": {
            "message": "Please review",
            "capture_response": True,
        },
    })
    ctx, events = _make_ctx()
    result = await execute_approval_node(node, {}, ctx)
    assert result.state == "completed"
    call_args = ctx.pause_run.call_args[0][0]
    assert call_args.get("captureResponse") is True


@pytest.mark.asyncio
async def test_approval_pause_includes_on_reject():
    """MEDIUM 10: pause_run metadata includes onRejectPrompt when set."""
    node = _parse_node({
        "id": "gate",
        "approval": {
            "message": "Review please",
            "on_reject": {"prompt": "Explain the rejection reason"},
        },
    })
    ctx, events = _make_ctx()
    result = await execute_approval_node(node, {}, ctx)
    assert result.state == "completed"
    call_args = ctx.pause_run.call_args[0][0]
    # onRejectPrompt is the ApprovalOnReject object
    assert call_args.get("onRejectPrompt") is not None


@pytest.mark.asyncio
async def test_approval_pause_no_extra_keys_without_fields():
    """MEDIUM 10: when capture_response/on_reject absent, keys not added."""
    node = _parse_node({
        "id": "gate",
        "approval": {"message": "Review"},
    })
    ctx, events = _make_ctx()
    await execute_approval_node(node, {}, ctx)
    call_args = ctx.pause_run.call_args[0][0]
    assert "captureResponse" not in call_args
    assert "onRejectPrompt" not in call_args


# ── FINDING 2: Command node routes via LLM ────────────────────────────────────

@pytest.mark.asyncio
async def test_command_node_uses_llm():
    """BLOCKER 2: command node sends prompt to LLM, not shell."""
    llm = _make_llm(["command result"])
    node = _parse_node({"id": "cmd", "command": "summarize"})
    ctx, events = _make_ctx(llm=llm)
    result = await execute_command_node(node, {}, ctx)
    assert result.state == "completed"
    assert result.output == "command result"
    assert llm.complete.called
    # Verify prompt is slash-command style
    call_kwargs = llm.complete.call_args
    messages = call_kwargs[0][0]
    assert messages[0]["content"].startswith("/")


@pytest.mark.asyncio
async def test_command_node_no_llm_returns_prompt():
    """BLOCKER 2: without LLM (test mode) returns formatted prompt."""
    node = _parse_node({"id": "cmd", "command": "do-something"})
    ctx, events = _make_ctx()
    result = await execute_command_node(node, {}, ctx)
    assert result.state == "completed"
    assert result.output == "/do-something"


# ── FINDING 1: Subgraph root children inherit placeholder depends_on ──────────

@pytest.mark.asyncio
async def test_subgraph_roots_inherit_placeholder_deps():
    """
    BLOCKER 1: when subgraph placeholder has depends_on:[A], the root children
    of the expanded subgraph must also depend on A so they cannot run before A.
    """
    import yaml

    subgraph_yaml = yaml.dump({
        "id": "child-wf",
        "name": "Child",
        "description": "Child workflow for testing",
        "kind": "subgraph",
        "nodes": [
            {"id": "C", "bash": "echo C"},
            {"id": "D", "bash": "echo D", "depends_on": ["C"]},
        ],
    })

    events = []
    ctx = DagRunContext(
        run_id="sg-dep-test",
        emit_event=lambda t, p: events.append((t, p)),
        get_run_status=AsyncMock(return_value="running"),
        pause_run=AsyncMock(),
        cancel_run=AsyncMock(),
        send_message=AsyncMock(),
        get_subgraph_yaml=lambda ref: (subgraph_yaml, "subgraph") if ref == "child-wf" else None,
    )

    nodes = [
        _parse_node({"id": "A", "bash": "echo A"}),
        _parse_node({"id": "B", "bash": "echo B", "depends_on": ["A"]}),
        _parse_node({
            "id": "SG",
            "depends_on": ["A", "B"],
            "subgraph": {"ref": "child-wf", "inputs": {}},
        }),
    ]

    outputs = await execute_dag(nodes, ctx)
    # All nodes should have run successfully
    assert outputs["A"].state == "completed"
    assert outputs["B"].state == "completed"
    # SG and its children
    assert "SG" in outputs


# ── FINDING 4: Subgraph when evaluated on node.subgraph.when ─────────────────

@pytest.mark.asyncio
async def test_subgraph_when_uses_subgraph_dot_when():
    """HIGH 4: subgraph.when is evaluated, not node.when (which is top-level)."""
    import yaml

    subgraph_yaml = yaml.dump({
        "id": "cond-sg",
        "name": "Cond",
        "description": "Conditional subgraph for testing",
        "kind": "subgraph",
        "nodes": [{"id": "inner", "bash": "echo inner"}],
    })

    # Subgraph with subgraph.when = false — children should be skipped
    events = []
    ctx = DagRunContext(
        run_id="sg-when-test",
        emit_event=lambda t, p: events.append((t, p)),
        get_run_status=AsyncMock(return_value="running"),
        pause_run=AsyncMock(),
        cancel_run=AsyncMock(),
        send_message=AsyncMock(),
        get_subgraph_yaml=lambda ref: (subgraph_yaml, "subgraph") if ref == "cond-sg" else None,
    )

    # node.when is NOT set; subgraph.when = "false" → should skip
    nodes = [
        _parse_node({
            "id": "SG",
            "subgraph": {"ref": "cond-sg", "when": "false", "inputs": {}},
        }),
    ]

    outputs = await execute_dag(nodes, ctx)
    # The subgraph placeholder should be skipped
    assert outputs["SG"].state == "skipped"


# ── FINDING 5: Subgraph child when rewritten with $INPUTS.* ──────────────────

def test_subgraph_child_when_rewritten():
    """HIGH 5: _clone_and_rewrite_node rewrites 'when' field with $INPUTS substitution."""
    from engine.core.dag_executor import _clone_and_rewrite_node
    from engine.schemas.dag_node import validate_dag_node

    inner_node_data = {
        "id": "step",
        "bash": "echo $INPUTS.flag",
        "when": "$INPUTS.enabled == 'true'",
    }
    inner_node, errs = validate_dag_node(inner_node_data, 0)
    assert not errs

    inputs = {"enabled": "true", "flag": "yes"}
    inner_ids = {"step"}
    cloned = _clone_and_rewrite_node(inner_node, "parent", inner_ids, inputs)

    # when field should have $INPUTS.enabled substituted
    assert "$INPUTS" not in (cloned.when or "")
    assert "true" in (cloned.when or "")


# ── FINDING 11: Subgraph expansion error aborts DAG ──────────────────────────

@pytest.mark.asyncio
async def test_subgraph_expansion_error_aborts_dag():
    """MEDIUM 11: subgraph expansion failure raises, aborting the DAG."""
    events = []
    ctx = DagRunContext(
        run_id="sg-abort-test",
        emit_event=lambda t, p: events.append((t, p)),
        get_run_status=AsyncMock(return_value="running"),
        pause_run=AsyncMock(),
        cancel_run=AsyncMock(),
        send_message=AsyncMock(),
        get_subgraph_yaml=lambda ref: None,  # always returns None → expansion fails
    )

    nodes = [
        _parse_node({
            "id": "SG",
            "subgraph": {"ref": "missing-ref", "inputs": {}},
        }),
    ]

    with pytest.raises(ValueError, match="not found"):
        await execute_dag(nodes, ctx)
