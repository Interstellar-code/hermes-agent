"""Harness for Matrix Coder: resolve a request and compose a specialist persona.

Explicit path (:func:`handle_trigger`):

  parse the user message for a trigger
    -> run the intake gate (explicit trigger -> always MATRIX)
    -> load base contracts + the role persona (+ the review lens text)
    -> compose persona text
    -> mark the dispatch active (so the ``pre_llm_call`` hook injects it this turn)
    -> return the composed persona string for same-turn injection.

Phase 5 implicit path (:func:`handle_implicit`):

  cheap coding-intent prefilter
    -> infer role + optional lens/domain
    -> right-size as DIRECT or MATRIX
    -> return a visible direct recommendation OR compose/activate the persona.

The Phase 0 :func:`run_passthrough` walking skeleton is retained for the
``/matrix`` smoke path and the older tests; it is no longer the main route.

Import-light by design: only depends on sibling ``core`` modules.
"""

from __future__ import annotations

import logging
from typing import Optional

from . import kanban_audit, registry
from .hermes_bridge import bridge
from .intake import ParsedInvocation, intake_gate, parse_trigger
from .intent_gate import (
    direct_recommendation_context,
    implicit_intake_gate,
    infer_implicit_invocation,
)
from .models import SpecialistResult, Verdict
from .prompts import compose_persona

logger = logging.getLogger(__name__)

_PASSTHROUGH = "_passthrough"


def _compose_and_activate(parsed: ParsedInvocation, session_id: Optional[str]) -> str:
    """Compose *parsed* into an active persona and open its audit card."""
    base = registry.load_base_contracts()
    persona = registry.load_persona(parsed.role)
    lens_text = (
        registry.load_lens(parsed.lens)
        if (parsed.role == "review" and parsed.lens)
        else None
    )
    domain_text = registry.load_domain(parsed.domain) if parsed.domain else None

    composed = compose_persona(base, persona, lens=lens_text, domain_pack=domain_text)

    # Prepend a visible activation marker and instruct the agent to echo it.
    lens_part = parsed.lens if parsed.lens else "none"
    marker = f"[matrix-coder active: role={parsed.role}, lens={lens_part}]"
    composed = (
        f"{marker}\n"
        "Begin your reply with the line above exactly as written.\n\n"
        + composed
    )

    bridge.set_active_persona(composed)

    cid = kanban_audit.open_card(parsed.role, parsed.lens, parsed.goal, session_id)
    bridge.set_active_card(cid)
    return composed


def handle_trigger(
    user_message: str, session_id: Optional[str] = None
) -> Optional[str]:
    """Parse *user_message*; if it triggers Matrix Coder, compose + activate.

    Returns the composed persona text (to be injected into the SAME turn) when
    a trigger is present, else ``None``. On the ``None`` path the caller is
    responsible for the defensive ``clear_active_persona`` (the hook does this);
    on the trigger path this sets the active persona via the bridge.

    Phase 2 audit-mirror: on a fresh trigger, any stale card left over from an
    interrupted prior turn is closed first (superseded), then a new ``running``
    audit card mirrors this invocation. Kanban failures are swallowed and must
    never stop persona composition/return.

    Defensive: never raises on the hot path — any error returns ``None``.
    """
    try:
        parsed = parse_trigger(user_message)
        if parsed is None:
            return None

        # A stale card from an interrupted prior turn -> close it as superseded
        # before opening the new one, so cards don't accumulate open forever.
        stale_id = bridge.active_card_id()
        if stale_id:
            kanban_audit.close_card(
                stale_id,
                summary="(superseded by a new matrix invocation)",
                status="done",
            )
            bridge.clear_active_card()

        # Explicit trigger -> intake gate always routes through the matrix.
        intake_gate(parsed)

        return _compose_and_activate(parsed, session_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: handle_trigger suppressed error: %s", exc)
        return None


def handle_implicit(
    user_message: str, session_id: Optional[str] = None
) -> Optional[str]:
    """Infer and right-size a plain coding request.

    MATRIX decisions silently compose and activate the inferred specialist.
    DIRECT decisions inject a visible recommendation/question without
    activating a persona or creating an audit card. Non-coding messages return
    ``None``. Explicit ``matrix ...`` triggers are excluded by the IntentGate.
    """
    try:
        parsed = infer_implicit_invocation(user_message)
        if parsed is None:
            return None
        decision = implicit_intake_gate(parsed)
        if decision.verdict is Verdict.DIRECT:
            return direct_recommendation_context(decision)
        return _compose_and_activate(parsed, session_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: handle_implicit suppressed error: %s", exc)
        return None


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
