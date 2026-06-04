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
    DOMAINS,
    ParsedInvocation,
    WORKFLOWS,
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


# -- parse_trigger: the six Phase-1.5 roles ---------------------------------

def test_parse_each_new_role():
    cases = {
        "matrix explore: map auth": ("explore", "map auth"),
        "matrix plan break down the export feature": (
            "plan",
            "break down the export feature",
        ),
        "matrix debug: why does login 500": ("debug", "why does login 500"),
        "matrix test add coverage for parser": (
            "test",
            "add coverage for parser",
        ),
        "matrix verify the fix landed": ("verify", "the fix landed"),
        "matrix simplify the auth helper": ("simplify", "the auth helper"),
    }
    for message, (role, goal) in cases.items():
        parsed = parse_trigger(message)
        assert parsed is not None, message
        assert parsed.role == role, message
        assert parsed.lens is None, message
        assert parsed.goal == goal, message


# -- parse_trigger: the four Phase-1.5 review lenses ------------------------

def test_parse_each_new_review_lens():
    cases = {
        "matrix review api: check the schema": "api",
        "matrix review performance: the hot loop": "performance",
        "matrix review quality: the new abstraction": "quality",
        "matrix review deps: the new package": "deps",
    }
    for message, lens in cases.items():
        parsed = parse_trigger(message)
        assert parsed is not None, message
        assert parsed.role == "review", message
        assert parsed.lens == lens, message
        assert parsed.goal, message


def test_parse_non_review_role_followed_by_lens_word_is_goal():
    # A lens only applies to review; for any other role the lens-word is goal.
    parsed = parse_trigger("matrix explore security")
    assert parsed.role == "explore"
    assert parsed.lens is None
    assert parsed.goal == "security"


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


# -- Phase 3: workflow parsing + intake gate --------------------------------

def test_workflows_constant_contains_four_names():
    assert WORKFLOWS == {"ralph", "autopilot", "ultrawork", "ultraqa"}


def test_parse_workflow_ralph():
    parsed = parse_trigger("matrix ralph: make the auth tests pass")
    assert parsed is not None
    assert parsed.role == "ralph"
    assert parsed.lens is None
    assert parsed.goal == "make the auth tests pass"


def test_parse_workflow_autopilot():
    parsed = parse_trigger("matrix autopilot: add a CSV export endpoint with tests")
    assert parsed is not None
    assert parsed.role == "autopilot"
    assert parsed.lens is None
    assert parsed.goal == "add a CSV export endpoint with tests"


def test_parse_workflow_ultrawork():
    parsed = parse_trigger("matrix ultrawork: refactor the three parser modules")
    assert parsed is not None
    assert parsed.role == "ultrawork"
    assert parsed.lens is None
    assert parsed.goal == "refactor the three parser modules"


def test_parse_workflow_ultraqa():
    parsed = parse_trigger("matrix ultraqa: get the integration suite green")
    assert parsed is not None
    assert parsed.role == "ultraqa"
    assert parsed.lens is None
    assert parsed.goal == "get the integration suite green"


def test_parse_workflow_plus_lens_word_treats_lens_as_goal():
    # Lenses never apply to workflows; a lens-word after a workflow is goal text.
    parsed = parse_trigger("matrix ralph security")
    assert parsed is not None
    assert parsed.role == "ralph"
    assert parsed.lens is None
    assert parsed.goal == "security"


def test_intake_gate_workflow_route():
    for name in ("ralph", "autopilot", "ultrawork", "ultraqa"):
        decision = intake_gate(ParsedInvocation(role=name, lens=None, goal="x"))
        assert decision.verdict is Verdict.MATRIX, name
        assert decision.proposed_route == name, name


# -- Phase 4: @domain token parsing -----------------------------------------

def test_domains_constant_contains_five_names():
    assert DOMAINS == {
        "frontend",
        "backend-api",
        "data-db",
        "infra-cli",
        "plugin-skill-authoring",
    }


def test_parse_domain_with_role():
    parsed = parse_trigger("matrix executor @backend-api: add a CSV export endpoint")
    assert parsed is not None
    assert parsed.role == "executor"
    assert parsed.domain == "backend-api"
    assert parsed.goal == "add a CSV export endpoint"
    assert parsed.lens is None


def test_parse_domain_with_review_and_lens():
    parsed = parse_trigger("matrix review security @frontend: audit the login form")
    assert parsed is not None
    assert parsed.role == "review"
    assert parsed.lens == "security"
    assert parsed.domain == "frontend"
    assert parsed.goal == "audit the login form"


def test_parse_domain_with_workflow():
    parsed = parse_trigger("matrix ralph @data-db: z")
    assert parsed is not None
    assert parsed.role == "ralph"
    assert parsed.domain == "data-db"
    assert parsed.goal == "z"
    assert parsed.lens is None


def test_parse_unknown_at_token_stays_in_goal():
    parsed = parse_trigger("matrix executor @bogus: x")
    assert parsed is not None
    assert parsed.role == "executor"
    assert parsed.domain is None
    assert parsed.goal == "@bogus: x"


def test_parse_domain_with_default_role():
    # @domain works with no explicit role token -> default role (review).
    parsed = parse_trigger("matrix @frontend: do work")
    assert parsed is not None
    assert parsed.role == "review"
    assert parsed.domain == "frontend"
    assert parsed.lens is None
    assert parsed.goal == "do work"


def test_parse_no_domain_gives_none():
    parsed = parse_trigger("matrix executor: add endpoint")
    assert parsed is not None
    assert parsed.domain is None
    assert parsed.goal == "add endpoint"


def test_parse_all_domain_names():
    for name in DOMAINS:
        msg = f"matrix executor @{name}: do work"
        parsed = parse_trigger(msg)
        assert parsed is not None, name
        assert parsed.domain == name, name
        assert parsed.goal == "do work", name


def test_intake_gate_domain_included_in_route():
    decision = intake_gate(
        ParsedInvocation(role="executor", lens=None, goal="x", domain="backend-api")
    )
    assert decision.verdict is Verdict.MATRIX
    assert decision.proposed_route == "executor@backend-api"


def test_intake_gate_review_lens_domain_route():
    decision = intake_gate(
        ParsedInvocation(role="review", lens="security", goal="y", domain="frontend")
    )
    assert decision.verdict is Verdict.MATRIX
    assert decision.proposed_route == "review:security@frontend"


def test_intake_gate_no_domain_route_unchanged():
    decision = intake_gate(
        ParsedInvocation(role="executor", lens=None, goal="x", domain=None)
    )
    assert decision.proposed_route == "executor"
