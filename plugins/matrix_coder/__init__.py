"""matrix_coder plugin — a specialist-coder layer for Hermes.

Matrix Coder turns a generic Hermes subagent into a focused *specialist* by
composing a PERSONA (text) into the child's context and re-asserting it per
turn via the ``pre_llm_call`` hook.  There is no subagent persona API — the
persona is pure text composition (see ``core/prompts.py``).

Phase 1 ships two usable roles invoked by an EXPLICIT trigger word ``matrix``
at the start of a user message (parsed in ``core/intake.py``, composed by
``core/harness.handle_trigger`` and injected this turn by the ``pre_llm_call``
hook):

* ``review`` (lenses: security, code) — read-only specialist reviewer,
* ``executor`` — surgical implementer (the role that edits files).

The remaining roles (explore, plan, debug, test, verify, simplify) arrive in
later phases.  This package ships:

* the plugin entrypoint + manifest,
* the shared ``_base/`` specialist contracts, the real ``review`` / ``executor``
  personas, the ``review-lenses/`` lens texts, and the ``_passthrough`` smoke
  persona,
* the ``core/`` package (models, config, intake, registry, prompts,
  hermes_bridge, harness, reporting),
* a ``/matrix`` STATUS/HELP command (no longer the trigger path).

Guardrail: single-writer-per-file (no file edited by two agents at once) is
enforced at orchestration time via disjoint file assignment / worktree
isolation; the per-role read/write nature in the boundary table is ADVISORY
persona guidance, not a hook-enforced block.  ``core/hermes_bridge.py`` holds
the file-claim bookkeeping that future enforcement will build on.

Hooks registered here are SYNC, take ``**kwargs``, and are defensive — they
never raise on the hot path.  All real logic lives in ``core/``.

Tracks epic issue #76.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .core import harness
from .core.hermes_bridge import bridge

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hooks (sync, defensive — never raise on the hot path)
# ---------------------------------------------------------------------------

def _inject_persona(**kwargs: Any) -> Optional[str]:
    """``pre_llm_call`` hook: parse the trigger and inject the persona this turn.

    Leak-proof lifecycle: parse the ``user_message`` kwarg. If it carries a
    Matrix Coder trigger, :func:`harness.handle_trigger` activates the composed
    persona and returns it for SAME-turn injection. If there is NO trigger, we
    defensively clear any active persona and return ``None`` — so a persona is
    active ONLY on the turn whose message carried the trigger.
    """
    try:
        user_message = kwargs.get("user_message", "") or ""
        composed = harness.handle_trigger(user_message=user_message)
        if composed:
            return composed
        # No trigger this turn -> defensive clear so nothing leaks forward.
        bridge.clear_active_persona()
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: _inject_persona suppressed error: %s", exc)
        return None


def _clear_persona(**kwargs: Any) -> Optional[str]:
    """``post_llm_call`` hook: backstop clear of the active persona.

    Secondary guard only. The PRIMARY guarantee is the unconditional clear in
    :func:`_inject_persona` at the start of every non-trigger turn. This
    backstop fires after a completed, non-interrupted turn (the core gates
    ``post_llm_call`` on ``final_response and not interrupted``), so it does NOT
    run on interrupted/empty turns — leak-proofness does not depend on it.
    Defensive — never raises.
    """
    try:
        bridge.clear_active_persona()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: _clear_persona suppressed error: %s", exc)
    return None


def _normalize_output(**kwargs: Any) -> Optional[str]:
    """``transform_llm_output`` hook: normalize specialist output.

    Phase 1: no-op.  Returns ``None`` (leave output unchanged) unless a Matrix
    Coder dispatch is active — and even then, Phase 1 has no transform to apply,
    so it returns ``None``.  The active-dispatch check is wired now so later
    phases can shape output without re-plumbing the hook.
    """
    try:
        if not bridge.is_active():
            return None
        # Phase 1: no transformation yet.
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: _normalize_output suppressed error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Slash command
# ---------------------------------------------------------------------------

_HELP_TEXT = (
    "Matrix Coder — specialist coder layer\n\n"
    "Invoke by starting your message with the trigger word `matrix`:\n"
    "  matrix <role> [<lens>] [:] <goal...>\n\n"
    "Roles:\n"
    "  review [security|code]   — read-only specialist reviewer (default role)\n"
    "  executor                 — surgical implementer (the role that edits files)\n\n"
    "Examples:\n"
    "  matrix review security: check auth in login.py\n"
    "  matrix executor add a CSV export endpoint\n"
    "  matrix is this safe?            (defaults to review)\n\n"
    "The `/matrix` command is STATUS/HELP only — it is not the trigger path.\n"
    "  /matrix          — this help\n"
    "  /matrix status   — whether a specialist persona is currently active"
)


def _handle_matrix(raw_args: str) -> Optional[str]:
    """``/matrix`` command: STATUS / HELP (no longer the trigger path).

    With no args, prints the available specialists + usage. ``/matrix status``
    reports whether a specialist persona is currently active. The actual trigger
    path is the ``matrix ...`` message handled by the ``pre_llm_call`` hook.
    """
    args = (raw_args or "").strip()
    try:
        if args.lower() == "status":
            active = bridge.is_active()
            return (
                "Matrix Coder status: persona ACTIVE for this turn."
                if active
                else "Matrix Coder status: no persona active."
            )
        return _HELP_TEXT
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: /matrix handler error: %s", exc)
        return f"[matrix_coder] error: {exc}"


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _inject_persona)
    ctx.register_hook("post_llm_call", _clear_persona)
    ctx.register_hook("transform_llm_output", _normalize_output)
    ctx.register_command(
        "matrix",
        handler=_handle_matrix,
        description="Matrix Coder status/help (trigger with a 'matrix ...' message).",
        args_hint="[status]",
    )
