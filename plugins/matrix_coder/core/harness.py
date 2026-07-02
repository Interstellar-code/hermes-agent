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
import re as _re
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

# Module-level constant: compiled once at import time (NIT fix).
_MARKER_SPOOF_RE = _re.compile(r"\[matrix-coder active:[^\]]*\]", _re.IGNORECASE)


def _safe_goal(goal: Optional[str]) -> str:
    """Sanitize goal text before storing in the audit card.

    Strips CR/LF, removes any embedded [matrix-coder active:...] marker-spoof
    substrings, and length-caps at 500 characters so no injected text can
    pollute the audit trail.
    """
    if not goal:
        return ""
    sanitized = goal.replace("\r", " ").replace("\n", " ")
    sanitized = _MARKER_SPOOF_RE.sub("", sanitized)
    return sanitized[:500].strip()


def _compose_and_activate(parsed: ParsedInvocation, session_id: Optional[str]) -> str:
    """Compose *parsed* into an active persona and open its audit card.

    Returns the composed persona string (no coercion marker prepended). The
    caller in __init__.py wraps this in {"context": ..., "target": "developer"}
    so the gateway delivers it in the system/trusted tier rather than the
    user-role message.
    """
    base = registry.load_base_contracts()
    persona = registry.load_persona(parsed.role)
    lens_text = (
        registry.load_lens(parsed.lens)
        if (parsed.role == "review" and parsed.lens)
        else None
    )
    domain_text = registry.load_domain(parsed.domain) if parsed.domain else None

    composed = compose_persona(base, persona, lens=lens_text, domain_pack=domain_text)

    # Audit log at INFO: record role/lens without pushing a marker through the
    # model. The coercion "Begin your reply with the line above..." was removed
    # in issue #140 — persona is now delivered in the trusted (system) tier.
    lens_part = parsed.lens if parsed.lens else "none"
    logger.info(
        "matrix_coder: activating persona role=%s lens=%s", parsed.role, lens_part
    )

    # bridge stores the raw composed text (not the dict wrapper)
    bridge.set_active_persona(composed, session_id)

    safe_goal = _safe_goal(parsed.goal)
    cid = kanban_audit.open_card(parsed.role, parsed.lens, safe_goal, session_id)
    bridge.set_active_card(cid, session_id)
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

        # Explicit trigger -> intake gate always routes through the matrix.
        intake_gate(parsed)

        # KB-2: capture any stale card BEFORE compose so we know what to close.
        # Then compose + activate (opens a NEW card). Only after the new card is
        # open do we close the stale one, so if _compose_and_activate raises the
        # stale card is NOT prematurely closed (no audit gap on failure).
        stale_id = bridge.active_card_id(session_id)

        result = _compose_and_activate(parsed, session_id)

        # Close the pre-existing stale card now that the new one is open.
        # Kanban failures are swallowed — must never block composition/return.
        if stale_id:
            try:
                kanban_audit.close_card(
                    stale_id,
                    summary="(superseded by a new matrix invocation)",
                    status="done",
                )
            except Exception:  # pragma: no cover - defensive
                pass

        return result
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("matrix_coder: handle_trigger suppressed error: %s", exc, exc_info=True)
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
        logger.warning("matrix_coder: handle_implicit suppressed error: %s", exc, exc_info=True)
        return None


def run_passthrough(goal: str, session_id: Optional[str] = None) -> SpecialistResult:
    """Run the Phase 0 passthrough specialist and return its shaped result.

    Composes the ``_passthrough`` persona, marks the dispatch active for its
    lifetime (so the persona-injection hook has something to return *during*
    the dispatch), and echoes *goal* back in the shared output contract. No LLM
    call is made.

    The active persona is scoped to this call via ``try/finally``: it is always
    cleared before returning, so the ``pre_llm_call`` hook correctly no-ops once
    the dispatch is over and never leaks into ordinary conversation turns.

    PL-3: ``_active_card_id`` is also cleared in the finally block so no stale
    card id lingers after passthrough returns.
    """
    base = registry.load_base_contracts()
    persona = registry.load_persona(_PASSTHROUGH)
    composed = compose_persona(base, persona)

    bridge.set_active_persona(composed, session_id)
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
        bridge.clear_active_persona(session_id)
        # PL-3: also clear any stale card id so no orphan reference lingers.
        bridge.clear_active_card(session_id)
