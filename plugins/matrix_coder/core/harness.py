"""Phase 0 walking-skeleton harness for Matrix Coder.

Wires the pieces end-to-end without a real LLM dispatch:

  load base contracts + the ``_passthrough`` persona
    -> compose persona text
    -> mark the dispatch active (so the ``pre_llm_call`` hook would inject it)
    -> return a :class:`SpecialistResult` shaped per the output contract.

Import-light by design: only depends on sibling ``core`` modules.
"""

from __future__ import annotations

import logging

from . import registry
from .hermes_bridge import bridge
from .models import IntakeDecision, SpecialistResult, Verdict
from .prompts import compose_persona

logger = logging.getLogger(__name__)

_PASSTHROUGH = "_passthrough"


def intake_gate(goal: str) -> IntakeDecision:
    """Decide whether *goal* is handled directly or routed through the matrix.

    Phase 0 stub: always route through the MATRIX so the walking skeleton is
    exercised.  Real heuristics arrive in a later phase.
    """
    return IntakeDecision(
        verdict=Verdict.MATRIX,
        reason="Phase 0 stub: all goals route through the matrix.",
        proposed_route=_PASSTHROUGH,
    )


def run_passthrough(goal: str) -> SpecialistResult:
    """Run the Phase 0 passthrough specialist and return its shaped result.

    Composes the ``_passthrough`` persona, marks the dispatch active for its
    lifetime (so the persona-injection hook has something to return *during*
    the dispatch), and echoes *goal* back in the shared output contract. No LLM
    call is made.

    The active persona is scoped to this call via ``try/finally``: it is always
    cleared before returning, so the ``pre_llm_call`` hook correctly no-ops once
    the dispatch is over and never leaks into ordinary conversation turns.
    """
    # Exercise the intake gate so the wired path matches the documented flow.
    intake_gate(goal)

    base = registry.load_base_contracts()
    persona = registry.load_persona(_PASSTHROUGH)
    composed = compose_persona(base, persona)

    bridge.set_active_persona(composed)
    try:
        return SpecialistResult(
            role=_PASSTHROUGH,
            findings=[],
            open_questions=[],
            positives=[f"Passthrough received the goal: {goal}"],
            recommendation=f"Echoed goal back (Phase 0 smoke test): {goal}",
            raw=None,
        )
    finally:
        # Dispatch is over — clear active state so injection no-ops afterwards.
        bridge.clear_active_persona()
