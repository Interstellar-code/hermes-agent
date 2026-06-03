"""Harness for Matrix Coder: parse a trigger and compose the specialist persona.

Phase 1 main path (:func:`handle_trigger`):

  parse the user message for a trigger
    -> run the intake gate (explicit trigger -> always MATRIX)
    -> load base contracts + the role persona (+ the review lens text)
    -> compose persona text
    -> mark the dispatch active (so the ``pre_llm_call`` hook injects it this turn)
    -> return the composed persona string for same-turn injection.

The Phase 0 :func:`run_passthrough` walking skeleton is retained for the
``/matrix`` smoke path and the older tests; it is no longer the main route.

Import-light by design: only depends on sibling ``core`` modules.
"""

from __future__ import annotations

import logging
from typing import Optional

from . import registry
from .hermes_bridge import bridge
from .intake import intake_gate, parse_trigger
from .models import SpecialistResult
from .prompts import compose_persona

logger = logging.getLogger(__name__)

_PASSTHROUGH = "_passthrough"


def handle_trigger(user_message: str) -> Optional[str]:
    """Parse *user_message*; if it triggers Matrix Coder, compose + activate.

    Returns the composed persona text (to be injected into the SAME turn) when
    a trigger is present, else ``None``. On the ``None`` path the caller is
    responsible for the defensive ``clear_active_persona`` (the hook does this);
    on the trigger path this sets the active persona via the bridge.

    Defensive: never raises on the hot path — any error returns ``None``.
    """
    try:
        parsed = parse_trigger(user_message)
        if parsed is None:
            return None

        # Explicit trigger -> intake gate always routes through the matrix.
        intake_gate(parsed)

        base = registry.load_base_contracts()
        persona = registry.load_persona(parsed.role)
        lens_text = (
            registry.load_lens(parsed.lens)
            if (parsed.role == "review" and parsed.lens)
            else None
        )

        composed = compose_persona(base, persona, lens=lens_text)
        bridge.set_active_persona(composed)
        return composed
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: handle_trigger suppressed error: %s", exc)
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
