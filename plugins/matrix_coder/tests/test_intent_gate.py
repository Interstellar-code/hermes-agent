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


# ---------------------------------------------------------------------------
# HIGH-1 regression: advisory phrase in TAIL must not suppress leading imperative
# ---------------------------------------------------------------------------

def test_high1_compound_sentence_imperative_wins():
    """'fix the bug, should i mention it also breaks login' — the imperative
    leads; trailing advisory clause must NOT suppress routing (HIGH-1 fix)."""
    parsed = infer_implicit_invocation(
        "fix the bug, should i mention it also breaks login"
    )
    assert parsed is not None, (
        "Compound sentence with leading imperative should ROUTE, not return None"
    )
    decision = implicit_intake_gate(parsed)
    assert decision.verdict is Verdict.MATRIX, (
        f"Expected MATRIX for compound imperative, got {decision.verdict}"
    )


def test_high1_compound_refactor_with_trailing_advisory_routes():
    """'refactor the codebase, tell me about edge cases after' — imperative
    leads; trailing advisory must not suppress (HIGH-1 fix)."""
    parsed = infer_implicit_invocation(
        "refactor the codebase, tell me about edge cases after"
    )
    assert parsed is not None, (
        "Compound imperative with trailing advisory should ROUTE, not return None"
    )
    decision = implicit_intake_gate(parsed)
    assert decision.verdict is Verdict.MATRIX, (
        f"Expected MATRIX for compound refactor imperative, got {decision.verdict}"
    )


def test_high1_leading_advisory_still_quiets():
    """Leading advisory phrases must still return None (no regression)."""
    assert infer_implicit_invocation("should I refactor the auth module?") is None
    assert infer_implicit_invocation("do you think the API design is good?") is None
    assert infer_implicit_invocation("can you explain how the database schema works?") is None


# ---------------------------------------------------------------------------
# MED-1 regression: purely interrogative yes/no questions must NOT be MATRIX
# ---------------------------------------------------------------------------

def test_med1_advisory_yesno_questions_not_matrix():
    """Purely interrogative yes/no questions about code are evaluative questions
    to the orchestrator, not specialist work — must not silently route MATRIX
    (MED-1 fix)."""
    advisory_questions = [
        "is this api backward compatible?",
        "did the fix work on the auth module?",
        "is the migration backward compatible?",
    ]
    for msg in advisory_questions:
        parsed = infer_implicit_invocation(msg)
        if parsed is not None:
            decision = implicit_intake_gate(parsed)
            assert decision.verdict is not Verdict.MATRIX, (
                f"Advisory yes/no question must not silently route MATRIX: {msg!r} "
                f"(got role={parsed.role}, verdict={decision.verdict})"
            )


def test_med1_debug_with_failure_signal_still_routes_matrix():
    """'why does the API endpoint crash?' has a failure signal ('crash') so it
    must STILL route MATRIX — MED-1 must not break this (regression guard)."""
    parsed = infer_implicit_invocation("why does the API endpoint crash?")
    assert parsed is not None, "'why does the API endpoint crash?' should ROUTE"
    assert parsed.role == "debug"
    decision = implicit_intake_gate(parsed)
    assert decision.verdict is Verdict.MATRIX, (
        f"Debug+failure-signal question must still be MATRIX, got {decision.verdict}"
    )


# ---------------------------------------------------------------------------
# MED-3 regression: addressee prefix must require word boundary + separator
# ---------------------------------------------------------------------------

def test_med3_hermespkg_routes_not_suppressed():
    """'hermespkg module needs a refactor' must ROUTE — the 'hermes' prefix is
    part of a compound identifier, not an addressee (MED-3 fix).
    Uses 'module' to ensure coding-intent signal is present."""
    parsed = infer_implicit_invocation("hermespkg module needs a refactor")
    assert parsed is not None, (
        "'hermespkg module needs a refactor' should ROUTE (not suppressed as addressee)"
    )


def test_med3_hermesutils_routes_not_suppressed():
    """'hermesutils.py has a bug' must ROUTE — 'hermes' is part of a filename
    which is a path/extension signal that satisfies coding intent."""
    parsed = infer_implicit_invocation("hermesutils.py has a bug")
    assert parsed is not None, (
        "'hermesutils.py has a bug' should ROUTE (not suppressed as addressee)"
    )


def test_med3_hermes_with_separator_quiets():
    """'hermes, what should we do about the API?' must QUIET — real addressee
    usage with comma separator (MED-3 fix should still suppress this)."""
    result = infer_implicit_invocation("hermes, what should we do about the API?")
    if result is not None:
        decision = implicit_intake_gate(result)
        assert decision.verdict is not Verdict.MATRIX, (
            "Orchestrator-addressed question should not silently route MATRIX"
        )


def test_plain_auth_safety_request_is_quiet():
    """'is this auth safe?' is advisory/interrogative — returns None by policy (#140)."""
    parsed = infer_implicit_invocation("is this auth safe?")
    assert parsed is None


def test_strong_security_request_routes_review_security():
    """A strong-signal security request still routes review:security."""
    parsed = infer_implicit_invocation("review the auth login flow for security vulnerabilities")

    assert parsed is not None
    assert parsed.role == "review"
    assert parsed.lens == "security"
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
        "is this safe to eat?",
        "how do I improve my running performance?",
        "add milk to my shopping list",
        "build me a workout plan",
        "check tomorrow's weather",
        "simplify this paragraph",
        "why does my car make that noise?",
    ]

    assert [m for m in ordinary_chat if infer_implicit_invocation(m)] == []


# ---------------------------------------------------------------------------
# #140 regression gate: advisory/meta/locate questions must NOT silently activate
# ---------------------------------------------------------------------------


def test_140_technical_advisory_to_orchestrator_is_quiet_or_ask():
    """These messages must not silently activate a Matrix specialist (#140)."""
    quiet_messages = [
        "what parts of the frontend SwitchUI profile creation need refactoring?",
        "should I refactor the auth module?",
        "where is the auth middleware?",
        "is the documentation up to date?",
        "hermes, what should we do about the API?",
        "do you think the API design is good?",
        "can you explain how the database schema works?",
    ]
    for msg in quiet_messages:
        result = infer_implicit_invocation(msg)
        # Policy: purely advisory/interrogative returns None (silent) OR at most
        # a DIRECT/ASK verdict (never silent MATRIX activation).
        if result is not None:
            decision = implicit_intake_gate(result)
            assert decision.verdict is not Verdict.MATRIX, (
                f"Message should not silently route MATRIX: {msg!r} "
                f"(got role={result.role}, verdict={decision.verdict})"
            )


def test_140_strong_signal_still_routes_matrix():
    """Imperative coding requests still get a MATRIX verdict."""
    strong_messages = [
        "refactor the auth module",
        "fix the login crash",
        "add tests for the parser",
        "review API endpoint performance",
    ]
    for msg in strong_messages:
        parsed = infer_implicit_invocation(msg)
        assert parsed is not None, f"Strong-signal message returned None: {msg!r}"
        decision = implicit_intake_gate(parsed)
        assert decision.verdict is Verdict.MATRIX, (
            f"Expected MATRIX verdict for: {msg!r} (got {decision.verdict})"
        )


# ---------------------------------------------------------------------------
# Kill-switch: MATRIX_CODER_IMPLICIT_ROUTING=0 disables implicit routing
# ---------------------------------------------------------------------------


def test_implicit_routing_killswitch_disables_implicit_only(monkeypatch):
    """With MATRIX_CODER_IMPLICIT_ROUTING=0, strong-signal implicit returns None;
    explicit 'matrix ...' trigger still fires.

    LOW-2 fix: load_config() reads os.environ fresh on every call — no reload
    needed.  We explicitly verify that toggling the env var mid-process flips
    _implicit_routing_enabled() WITHOUT any importlib.reload, pinning the
    fresh-read contract.
    """
    import matrix_coder as plugin
    from matrix_coder.core.hermes_bridge import bridge

    # Verify kill-switch is honoured by _implicit_routing_enabled() without
    # any module reload (pinning the fresh os.environ-read contract).
    monkeypatch.setenv("MATRIX_CODER_IMPLICIT_ROUTING", "0")
    assert plugin._implicit_routing_enabled() is False, (
        "_implicit_routing_enabled() should return False with env=0 (no reload needed)"
    )

    monkeypatch.delenv("MATRIX_CODER_IMPLICIT_ROUTING", raising=False)
    assert plugin._implicit_routing_enabled() is True, (
        "_implicit_routing_enabled() should return True when env var is absent (no reload needed)"
    )

    # Apply kill-switch and confirm the full inject path is suppressed.
    monkeypatch.setenv("MATRIX_CODER_IMPLICIT_ROUTING", "0")
    bridge.clear_active_persona()
    result_implicit = plugin._inject_persona(user_message="refactor the auth module")
    # Kill-switch: strong implicit must return None
    assert result_implicit is None, (
        f"Expected None with kill-switch, got: {result_implicit}"
    )

    bridge.clear_active_persona()
    result_explicit = plugin._inject_persona(user_message="matrix executor: add export")
    # Explicit trigger is unaffected by the kill-switch
    assert result_explicit is not None
    assert isinstance(result_explicit, dict)
    assert result_explicit.get("target") == "developer"

    bridge.clear_active_persona()
