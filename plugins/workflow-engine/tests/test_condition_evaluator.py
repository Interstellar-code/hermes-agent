"""
Tests for condition_evaluator.py — ports the grammar exactly from TS.

Includes fuzz-style golden tests: same input strings that TS test vectors cover.
"""

import pytest
from engine.core.condition_evaluator import evaluate_condition
from engine.schemas.workflow_run import make_node_output


def _outputs(*pairs) -> dict:
    """Build node_outputs dict from (node_id, output_text) pairs."""
    return {nid: make_node_output("completed", out) for nid, out in pairs}


# ── Basic equality ────────────────────────────────────────────────────────────

def test_string_eq_match():
    result, parsed = evaluate_condition(
        "$classify.output == 'BUG'",
        _outputs(("classify", "BUG")),
    )
    assert parsed is True
    assert result is True


def test_string_eq_no_match():
    result, parsed = evaluate_condition(
        "$classify.output == 'BUG'",
        _outputs(("classify", "FEATURE")),
    )
    assert parsed is True
    assert result is False


def test_string_ne_match():
    result, parsed = evaluate_condition(
        "$classify.output != 'ERROR'",
        _outputs(("classify", "OK")),
    )
    assert parsed is True
    assert result is True


def test_string_ne_no_match():
    result, parsed = evaluate_condition(
        "$classify.output != 'ERROR'",
        _outputs(("classify", "ERROR")),
    )
    assert parsed is True
    assert result is False


# ── Dot-notation field access ─────────────────────────────────────────────────

def test_dot_field_eq():
    import json
    result, parsed = evaluate_condition(
        "$node1.output.type == 'BUG'",
        _outputs(("node1", json.dumps({"type": "BUG", "severity": "high"}))),
    )
    assert parsed is True
    assert result is True


def test_dot_field_missing():
    import json
    result, parsed = evaluate_condition(
        "$node1.output.missing == 'X'",
        _outputs(("node1", json.dumps({"type": "BUG"}))),
    )
    # missing field → empty string → does not equal 'X' → False, but still parsed
    assert parsed is True
    assert result is False


# ── Numeric comparisons ───────────────────────────────────────────────────────

def test_numeric_gt():
    result, parsed = evaluate_condition(
        "$score.output > '80'",
        _outputs(("score", "90")),
    )
    assert parsed is True
    assert result is True


def test_numeric_lt():
    result, parsed = evaluate_condition(
        "$score.output < '50'",
        _outputs(("score", "30")),
    )
    assert parsed is True
    assert result is True


def test_numeric_ge():
    result, parsed = evaluate_condition(
        "$score.output >= '90'",
        _outputs(("score", "90")),
    )
    assert parsed is True
    assert result is True


def test_numeric_le():
    result, parsed = evaluate_condition(
        "$score.output <= '100'",
        _outputs(("score", "100")),
    )
    assert parsed is True
    assert result is True


def test_numeric_non_numeric_fail_closed():
    result, parsed = evaluate_condition(
        "$score.output > '80'",
        _outputs(("score", "NOT_A_NUMBER")),
    )
    # fail-closed: non-numeric → parsed=False
    assert parsed is False
    assert result is False


# ── Compound AND / OR ─────────────────────────────────────────────────────────

def test_compound_and_both_true():
    result, parsed = evaluate_condition(
        "$a.output == 'X' && $b.output != 'Y'",
        _outputs(("a", "X"), ("b", "Z")),
    )
    assert parsed is True
    assert result is True


def test_compound_and_one_false():
    result, parsed = evaluate_condition(
        "$a.output == 'X' && $b.output != 'Y'",
        _outputs(("a", "X"), ("b", "Y")),
    )
    assert parsed is True
    assert result is False


def test_compound_or_first_true():
    result, parsed = evaluate_condition(
        "$a.output == 'X' || $b.output == 'Y'",
        _outputs(("a", "X"), ("b", "Z")),
    )
    assert parsed is True
    assert result is True


def test_compound_or_second_true():
    result, parsed = evaluate_condition(
        "$a.output == 'NOPE' || $b.output == 'Y'",
        _outputs(("a", "X"), ("b", "Y")),
    )
    assert parsed is True
    assert result is True


def test_compound_or_both_false():
    result, parsed = evaluate_condition(
        "$a.output == 'NOPE' || $b.output == 'NOPE'",
        _outputs(("a", "X"), ("b", "Y")),
    )
    assert parsed is True
    assert result is False


def test_and_higher_precedence_than_or():
    # ($a=='X' && $b=='Y') || $c=='Z'  → (True && False) || True → True
    result, parsed = evaluate_condition(
        "$a.output == 'X' && $b.output == 'Y' || $c.output == 'Z'",
        _outputs(("a", "X"), ("b", "WRONG"), ("c", "Z")),
    )
    assert parsed is True
    assert result is True


# ── Fail-closed on bad syntax ─────────────────────────────────────────────────

def test_unparseable_expression():
    result, parsed = evaluate_condition(
        "this is not valid",
        _outputs(),
    )
    assert parsed is False
    assert result is False


def test_unknown_node_ref():
    # Unknown node → empty string → does not equal 'X' → False, still parsed
    result, parsed = evaluate_condition(
        "$unknown.output == 'X'",
        _outputs(),
    )
    assert parsed is True
    assert result is False


def test_empty_string_match():
    result, parsed = evaluate_condition(
        "$node.output == ''",
        _outputs(("node", "")),
    )
    assert parsed is True
    assert result is True


# ── Whitespace tolerance ──────────────────────────────────────────────────────

def test_whitespace_around_operator():
    result, parsed = evaluate_condition(
        "  $a.output   ==   'hello'  ",
        _outputs(("a", "hello")),
    )
    assert parsed is True
    assert result is True


# ── Single-quoted value with spaces ──────────────────────────────────────────

def test_value_with_spaces():
    result, parsed = evaluate_condition(
        "$node.output == 'hello world'",
        _outputs(("node", "hello world")),
    )
    assert parsed is True
    assert result is True
