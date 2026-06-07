"""Phase 5 implicit routing / IntentGate tests."""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from core.intent_gate import (
    direct_recommendation_context,
    implicit_intake_gate,
    infer_implicit_invocation,
)  # noqa: E402
from core.models import Verdict  # noqa: E402


def test_plain_auth_safety_request_routes_review_security():
    parsed = infer_implicit_invocation("is this auth safe?")

    assert parsed is not None
    assert parsed.role == "review"
    assert parsed.lens == "security"
    assert parsed.domain is None
    assert implicit_intake_gate(parsed).proposed_route == "review:security"


def test_debug_and_domain_are_inferred():
    parsed = infer_implicit_invocation("why does the API endpoint crash?")

    assert parsed is not None
    assert parsed.role == "debug"
    assert parsed.lens is None
    assert parsed.domain == "backend-api"


def test_explicit_trigger_is_never_implicitly_reclassified():
    assert infer_implicit_invocation("matrix executor: is this auth safe?") is None


def test_plain_explicit_role_naming_wins_over_general_inference():
    parsed = infer_implicit_invocation("review this for performance")

    assert parsed is not None
    assert parsed.role == "review"
    assert parsed.lens == "performance"


def test_trivial_low_risk_mechanical_request_recommends_direct():
    parsed = infer_implicit_invocation("fix README typo")
    decision = implicit_intake_gate(parsed)

    assert decision.verdict is Verdict.DIRECT
    assert decision.proposed_route is None
    context = direct_recommendation_context(decision)
    assert "<verdict>direct</verdict>" in context
    assert "Invoke Matrix Coder anyway" in context


def test_direct_acceptance_does_not_repeat_the_recommendation():
    assert infer_implicit_invocation("let Hermes handle the README typo directly") is None


def test_short_sensitive_request_still_routes_matrix():
    parsed = infer_implicit_invocation("fix auth bug")
    decision = implicit_intake_gate(parsed)

    assert decision.verdict is Verdict.MATRIX
    assert decision.proposed_route == "executor"


def test_review_lens_and_domain_route_are_composed():
    parsed = infer_implicit_invocation("review API endpoint performance")
    decision = implicit_intake_gate(parsed)

    assert decision.verdict is Verdict.MATRIX
    assert decision.proposed_route == "review:performance@backend-api"


def test_false_positive_budget_is_zero_for_non_coding_corpus():
    # Acceptance budget: 0 false positives across this representative corpus.
    ordinary_chat = [
        "plan a weekend trip",
        "review my résumé",
        "is this food safe?",
        "test my microphone",
        "debug my relationship",
        "where is the nearest pharmacy?",
        "how do I improve my running performance?",
        "add milk to my shopping list",
        "build me a workout plan",
        "check tomorrow's weather",
        "simplify this paragraph",
        "why does my car make that noise?",
    ]

    assert [m for m in ordinary_chat if infer_implicit_invocation(m)] == []
