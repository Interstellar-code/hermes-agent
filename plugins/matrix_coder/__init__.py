"""matrix_coder plugin — a specialist-coder layer for Hermes.

Matrix Coder turns a generic Hermes subagent into a focused *specialist* by
composing a PERSONA (text) into the child's context and re-asserting it per
turn via the ``pre_llm_call`` hook.  There is no subagent persona API — the
persona is pure text composition (see ``core/prompts.py``).

The 8 roles (explore, plan, executor, review, debug, test, verify, simplify)
arrive in Phase 1/1.5.  This Phase 0 scaffold ships only:

* the plugin entrypoint + manifest,
* the shared ``_base/`` specialist contracts and ONE ``_passthrough`` persona,
* the ``core/`` package (models, config, registry, prompts, hermes_bridge,
  harness, reporting),
* a walking-skeleton ``/matrix`` command that runs the passthrough harness.

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

from .core import harness, reporting
from .core.hermes_bridge import bridge

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hooks (sync, defensive — never raise on the hot path)
# ---------------------------------------------------------------------------

def _inject_persona(**kwargs: Any) -> Optional[str]:
    """``pre_llm_call`` hook: inject the active specialist persona text.

    Phase 0: if a Matrix Coder dispatch is active, return the composed persona
    so it is re-asserted on this turn; otherwise return ``None`` (no-op).
    """
    try:
        return bridge.inject_persona_text()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: _inject_persona suppressed error: %s", exc)
        return None


def _normalize_output(**kwargs: Any) -> Optional[str]:
    """``transform_llm_output`` hook: normalize specialist output.

    Phase 0: no-op.  Returns ``None`` (leave output unchanged) unless a Matrix
    Coder dispatch is active — and even then, Phase 0 has no transform to apply,
    so it returns ``None``.  The active-dispatch check is wired now so later
    phases can shape output without re-plumbing the hook.
    """
    try:
        if not bridge.is_active():
            return None
        # Phase 0: no transformation yet.
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: _normalize_output suppressed error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Slash command
# ---------------------------------------------------------------------------

def _handle_matrix(raw_args: str) -> Optional[str]:
    """``/matrix`` command: Phase 0 runs the passthrough harness.

    The goal is the trailing free-form text.  Returns the result rendered as
    markdown per the shared output contract.
    """
    goal = (raw_args or "").strip()
    if not goal:
        return (
            "/matrix — Matrix Coder (Phase 0 scaffold)\n\n"
            "Usage: /matrix <goal>\n"
            "Example: /matrix review this for security\n\n"
            "Phase 0 runs a passthrough that echoes the goal back in the "
            "shared output contract."
        )
    try:
        result = harness.run_passthrough(goal)
        return reporting.render_markdown(result)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: /matrix handler error: %s", exc)
        return f"[matrix_coder] error: {exc}"


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _inject_persona)
    ctx.register_hook("transform_llm_output", _normalize_output)
    ctx.register_command(
        "matrix",
        handler=_handle_matrix,
        description="Matrix Coder specialist dispatch (Phase 0 scaffold).",
        args_hint="<goal>",
    )
