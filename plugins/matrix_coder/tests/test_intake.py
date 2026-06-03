"""Tests for the Phase 1 invocation parser + intake gate (``core.intake``).

These import the ``core`` package directly via sys.path manipulation (matching
the walking-skeleton tests) to stay independent of the Hermes loader. They
cover:

1. ``parse_trigger`` across the whole grammar — no trigger, default role,
   explicit roles, review lenses, the optional ``:`` separator, case folding.
2. ``looks_sensitive`` for sensitive and benign goals.
3. ``intake_gate`` always returning a MATRIX verdict with the correct route for
   an explicit trigger.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from core.intake import (  # noqa: E402
    ParsedInvocation,
    intake_gate,
    looks_sensitive,
    parse_trigger,
)
from core.models import Verdict  # noqa: E402


# -- parse_trigger: no trigger ----------------------------------------------

def test_parse_no_trigger_returns_none():
    assert parse_trigger("just a normal message") is None
    assert parse_trigger("please review login.py") is None
    assert parse_trigger("") is None
    assert parse_trigger("   ") is None


def test_parse_trigger_must_be_first_token():
    # "matrix" elsewhere in the message is not a trigger.
    assert parse_trigger("can you matrix review this?") is None


# -- parse_trigger: roles + lenses ------------------------------------------

def test_parse_review_with_security_lens_and_colon():
    parsed = parse_trigger("matrix review security: check auth in login.py")
    assert isinstance(parsed, ParsedInvocation)
    assert parsed.role == "review"
    assert parsed.lens == "security"
    assert parsed.goal == "check auth in login.py"


def test_parse_review_with_code_lens_no_colon():
    parsed = parse_trigger("matrix review code refactor the parser")
    assert parsed.role == "review"
    assert parsed.lens == "code"
    assert parsed.goal == "refactor the parser"


def test_parse_executor_no_lens():
    parsed = parse_trigger("matrix executor add a CSV export endpoint")
    assert parsed.role == "executor"
    assert parsed.lens is None
    assert parsed.goal == "add a CSV export endpoint"


def test_parse_executor_lens_token_is_part_of_goal():
    # A lens only applies to review; for executor the "security" token is goal.
    parsed = parse_trigger("matrix executor security hardening")
    assert parsed.role == "executor"
    assert parsed.lens is None
    assert parsed.goal == "security hardening"


def test_parse_default_role_when_first_token_not_a_role():
    parsed = parse_trigger("matrix is this safe?")
    assert parsed.role == "review"  # default role
    assert parsed.lens is None
    assert parsed.goal == "is this safe?"


def test_parse_case_insensitive_trigger_and_role():
    parsed = parse_trigger("MATRIX Review Security: look here")
    assert parsed.role == "review"
    assert parsed.lens == "security"
    assert parsed.goal == "look here"


def test_parse_trigger_only_no_body():
    parsed = parse_trigger("matrix")
    assert parsed.role == "review"
    assert parsed.lens is None
    assert parsed.goal == ""


def test_parse_review_lens_only_empty_goal():
    parsed = parse_trigger("matrix review security")
    assert parsed.role == "review"
    assert parsed.lens == "security"
    assert parsed.goal == ""


# -- looks_sensitive --------------------------------------------------------

def test_looks_sensitive_flags_security_goals():
    assert looks_sensitive("check auth in login.py") is True
    assert looks_sensitive("rotate the API key") is True
    assert looks_sensitive("update the migration") is True
    assert looks_sensitive("edit the github actions workflow") is True


def test_looks_sensitive_benign_goal():
    assert looks_sensitive("add a CSV export endpoint") is False
    assert looks_sensitive("") is False


# -- intake_gate ------------------------------------------------------------

def test_intake_gate_review_with_lens_route():
    decision = intake_gate(
        ParsedInvocation(role="review", lens="security", goal="x")
    )
    assert decision.verdict is Verdict.MATRIX
    assert decision.proposed_route == "review:security"


def test_intake_gate_review_without_lens_route():
    decision = intake_gate(ParsedInvocation(role="review", lens=None, goal="x"))
    assert decision.verdict is Verdict.MATRIX
    assert decision.proposed_route == "review"


def test_intake_gate_executor_route():
    decision = intake_gate(
        ParsedInvocation(role="executor", lens=None, goal="add endpoint")
    )
    assert decision.verdict is Verdict.MATRIX
    assert decision.proposed_route == "executor"
