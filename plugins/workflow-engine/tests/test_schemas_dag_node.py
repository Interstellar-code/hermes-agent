"""
test_schemas_dag_node — round-trip tests for all DagNode variants and validate_dag_node().
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from engine.schemas.dag_node import (
    validate_dag_node,
    CommandNode, PromptNode, BashNode, LoopNode,
    ApprovalNode, CancelNode, ScriptNode, SubgraphNode,
    is_bash_node, is_loop_node, is_approval_node, is_cancel_node,
    is_script_node, is_subgraph_node, is_trigger_rule,
    TRIGGER_RULES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ok(raw):
    node, errors = validate_dag_node(raw, 0)
    assert errors == [], f"Unexpected errors: {errors}"
    assert node is not None
    return node


def fail(raw):
    node, errors = validate_dag_node(raw, 0)
    assert errors, "Expected validation errors but got none"
    assert node is None
    return errors


# ---------------------------------------------------------------------------
# CommandNode
# ---------------------------------------------------------------------------

def test_command_node():
    node = ok({"id": "run-tests", "command": "run-tests"})
    assert isinstance(node, CommandNode)
    assert node.command == "run-tests"


def test_command_node_invalid_name():
    errors = fail({"id": "x", "command": "bad name!"})
    assert any("invalid command name" in e for e in errors)


# ---------------------------------------------------------------------------
# PromptNode
# ---------------------------------------------------------------------------

def test_prompt_node():
    node = ok({"id": "ask", "prompt": "What is the answer?"})
    assert isinstance(node, PromptNode)


def test_prompt_empty_fails():
    errors = fail({"id": "ask", "prompt": ""})
    assert errors


# ---------------------------------------------------------------------------
# BashNode
# ---------------------------------------------------------------------------

def test_bash_node():
    node = ok({"id": "check", "bash": "echo hello"})
    assert isinstance(node, BashNode)
    assert is_bash_node(node)


def test_bash_node_negative_timeout_fails():
    errors = fail({"id": "x", "bash": "echo hi", "timeout": -1})
    assert any("timeout" in e for e in errors)


def test_bash_node_valid_timeout():
    node = ok({"id": "x", "bash": "sleep 1", "timeout": 5000})
    assert isinstance(node, BashNode)
    assert node.timeout == 5000


# ---------------------------------------------------------------------------
# ScriptNode
# ---------------------------------------------------------------------------

def test_script_node_bun():
    node = ok({"id": "s", "script": "console.log('hi')", "runtime": "bun"})
    assert isinstance(node, ScriptNode)
    assert is_script_node(node)


def test_script_node_uv():
    node = ok({"id": "s", "script": "print('hi')", "runtime": "uv"})
    assert isinstance(node, ScriptNode)


def test_script_missing_runtime_fails():
    errors = fail({"id": "s", "script": "print('hi')"})
    assert any("runtime" in e for e in errors)


# ---------------------------------------------------------------------------
# LoopNode
# ---------------------------------------------------------------------------

def test_loop_node():
    node = ok({
        "id": "loop1",
        "loop": {
            "prompt": "Do work",
            "until": "COMPLETE",
            "max_iterations": 5,
        },
    })
    assert isinstance(node, LoopNode)
    assert is_loop_node(node)
    assert node.loop.max_iterations == 5


def test_loop_with_retry_fails():
    errors = fail({
        "id": "loop1",
        "loop": {"prompt": "p", "until": "DONE", "max_iterations": 3},
        "retry": {"max_attempts": 2},
    })
    assert any("retry" in e for e in errors)


def test_loop_interactive_requires_gate_message():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        from engine.schemas.loop import LoopNodeConfig
        LoopNodeConfig.model_validate({
            "prompt": "p", "until": "DONE", "max_iterations": 3,
            "interactive": True,
        })


# ---------------------------------------------------------------------------
# ApprovalNode
# ---------------------------------------------------------------------------

def test_approval_node():
    node = ok({
        "id": "gate",
        "approval": {"message": "Please review and approve."},
    })
    assert isinstance(node, ApprovalNode)
    assert is_approval_node(node)


def test_approval_missing_message_fails():
    from pydantic import ValidationError
    with pytest.raises((ValidationError, Exception)):
        ok({"id": "gate", "approval": {"message": ""}})


# ---------------------------------------------------------------------------
# CancelNode
# ---------------------------------------------------------------------------

def test_cancel_node():
    node = ok({"id": "abort", "cancel": "Prerequisites not met"})
    assert isinstance(node, CancelNode)
    assert is_cancel_node(node)


# ---------------------------------------------------------------------------
# SubgraphNode
# ---------------------------------------------------------------------------

def test_subgraph_node():
    node = ok({"id": "expand", "subgraph": {"ref": "my-subgraph"}})
    assert isinstance(node, SubgraphNode)
    assert is_subgraph_node(node)


def test_subgraph_empty_ref_fails():
    errors = fail({"id": "expand", "subgraph": {"ref": ""}})
    assert errors


# ---------------------------------------------------------------------------
# Mutual exclusivity
# ---------------------------------------------------------------------------

def test_two_modes_fails():
    errors = fail({"id": "x", "command": "foo", "prompt": "bar"})
    assert any("mutually exclusive" in e for e in errors)


def test_no_mode_fails():
    errors = fail({"id": "x"})
    assert errors


def test_missing_id_fails():
    errors = fail({"prompt": "hello"})
    assert any("id" in e for e in errors)


# ---------------------------------------------------------------------------
# TriggerRule
# ---------------------------------------------------------------------------

def test_trigger_rule_valid():
    node = ok({"id": "x", "prompt": "hi", "trigger_rule": "all_success"})
    assert node.trigger_rule == "all_success"


def test_is_trigger_rule():
    for rule in TRIGGER_RULES:
        assert is_trigger_rule(rule)
    assert not is_trigger_rule("bad_rule")


# ---------------------------------------------------------------------------
# depends_on / common base fields
# ---------------------------------------------------------------------------

def test_depends_on_field():
    node = ok({"id": "b", "prompt": "do b", "depends_on": ["a"]})
    assert node.depends_on == ["a"]


def test_idle_timeout_must_be_positive():
    errors = fail({"id": "x", "prompt": "hi", "idle_timeout": -1})
    assert any("idle_timeout" in e for e in errors)


def test_retry_config():
    node = ok({
        "id": "x",
        "prompt": "hi",
        "retry": {"max_attempts": 3, "delay_ms": 2000, "on_error": "all"},
    })
    assert node.retry is not None
    assert node.retry.max_attempts == 3


def test_thinking_shorthand_string():
    node = ok({"id": "x", "prompt": "think", "thinking": "adaptive"})
    assert node.thinking is not None
