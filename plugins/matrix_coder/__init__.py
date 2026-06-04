"""matrix_coder plugin — a specialist-coder layer for Hermes.

Matrix Coder turns a generic Hermes subagent into a focused *specialist* by
composing a PERSONA (text) into the child's context and re-asserting it per
turn via the ``pre_llm_call`` hook.  There is no subagent persona API — the
persona is pure text composition (see ``core/prompts.py``).

Roles are invoked by an EXPLICIT trigger word ``matrix`` at the start of a user
message (parsed in ``core/intake.py``, composed by
``core/harness.handle_trigger`` and injected this turn by the ``pre_llm_call``
hook):

* ``review`` (lenses: security, code, api, performance, quality, deps) —
  read-only specialist reviewer (default role),
* ``executor`` — surgical implementer (the one role that edits files),
* ``explore`` — read-only territory mapper (files, flows, deps, risks),
* ``plan`` — read-only planner (dependency-aware tasks + design + go/no-go),
* ``debug`` — read-only root-cause hunter (proposes a fix strategy),
* ``test`` — adds/strengthens tests (edits test files when asked),
* ``verify`` — read-only evidence auditor (pass/fail ledger),
* ``simplify`` — behavior-preserving reducer (edits when asked).

Workflow skills compose the specialist roles into multi-step procedures and
instruct the live Hermes agent (which IS the parent agent with full tool
access) to execute the loop. No lens applies to workflows.

* ``ralph`` — iterative executor→verify loop until pass or cap (5 iterations),
* ``autopilot`` — full end-to-end plan→executor→test→review→verify chain,
* ``ultrawork`` — parallel fan-out via delegate_task with disjoint file sets,
* ``ultraqa`` — test→verify→fix cycle until suite is green or cap (5 cycles).

Domain packs (Phase 4) are composable context layers that add stack-specific
conventions, checklists, and pitfalls ON TOP OF any role or workflow — they
do NOT change the role's contract or output format. Specify with ``@<name>``:

* ``@frontend`` — UI/UX, components, state, a11y, browser, bundling,
* ``@backend-api`` — HTTP design, contracts, validation, auth, persistence,
* ``@data-db`` — schema/migrations, queries, indexing, transactions, N+1,
* ``@infra-cli`` — CLI ergonomics, packaging, config/env, deployment, observability,
* ``@plugin-skill-authoring`` — Hermes plugin/skill authoring conventions.

This package ships:

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

from .core import harness, kanban_audit
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

    Phase 2 audit-mirror: on the non-trigger path, any still-open audit card is
    an orphan (a prior dispatch never produced a completion signal). We close it
    ``done`` and clear the bookkeeping — self-correcting cleanup mirroring the
    persona leak guard. Kanban failures are swallowed.
    """
    try:
        user_message = kwargs.get("user_message", "") or ""
        composed = harness.handle_trigger(
            user_message=user_message, session_id=kwargs.get("session_id")
        )
        if composed:
            return composed
        # No trigger this turn -> close any orphan card, then defensive clear so
        # neither a persona nor a card leaks forward.
        orphan_id = bridge.active_card_id()
        if orphan_id:
            kanban_audit.close_card(
                orphan_id,
                summary="(closed: no completion signal)",
                status="done",
            )
            bridge.clear_active_card()
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

    Phase 2 audit-mirror: this is the normal close path for a card opened on the
    trigger turn — close it ``done`` with the assistant's response as the
    summary, then clear the card bookkeeping. Defensive — never raises.
    """
    try:
        card_id = bridge.active_card_id()
        if card_id:
            kanban_audit.close_card(
                card_id,
                summary=kwargs.get("assistant_response"),
                status="done",
            )
            bridge.clear_active_card()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: _clear_persona card-close suppressed error: %s", exc)
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
    "  matrix <role> [<lens>] [@<domain>] [:] <goal...>\n\n"
    "Roles:\n"
    "  review [<lens>]  — read-only specialist reviewer (default role)\n"
    "  executor         — surgical implementer (the one role that edits files)\n"
    "  explore          — read-only: map files, flows, dependencies, risks\n"
    "  plan             — read-only: dependency-aware tasks + design + go/no-go\n"
    "  debug            — read-only: isolate root cause, propose a fix strategy\n"
    "  test             — add/strengthen tests (edits test files when asked)\n"
    "  verify           — read-only: pass/fail evidence ledger for claims\n"
    "  simplify         — behavior-preserving reduction (edits when asked)\n\n"
    "Review lenses (only apply to `review`):\n"
    "  security     — auth, injection, secrets, unsafe defaults, crypto\n"
    "  code         — general correctness + maintainability (default review)\n"
    "  api          — compatibility, schema drift, error semantics, versioning\n"
    "  performance  — hot paths, N+1, algorithmic cost, allocation/I-O, caching\n"
    "  quality      — logic defects, SOLID, brittle abstractions, anti-patterns\n"
    "  deps         — package health, licenses, CVEs, pinning, supply-chain\n\n"
    "Domain packs (add stack context on top of any role or workflow):\n"
    "  @frontend              — components, state, a11y, browser, bundling\n"
    "  @backend-api           — HTTP design, contracts, validation, auth, persistence\n"
    "  @data-db               — schema/migrations, queries, indexing, transactions, N+1\n"
    "  @infra-cli             — CLI ergonomics, packaging, config/env, deployment, observability\n"
    "  @plugin-skill-authoring — Hermes plugin/skill authoring conventions\n\n"
    "Workflows (multi-step procedures; no lens applies):\n"
    "  ralph       — loop executor→verify until pass or 5-iteration cap\n"
    "  autopilot   — full chain plan→executor→test→review→verify end-to-end\n"
    "  ultrawork   — fan-out via delegate_task with disjoint file sets, then aggregate\n"
    "  ultraqa     — cycle test→verify→fix until suite is green or 5-cycle cap\n\n"
    "Workflow examples:\n"
    "  matrix ralph: make the auth tests pass\n"
    "  matrix autopilot: add a CSV export endpoint with tests\n"
    "  matrix ultrawork: refactor the three parser modules\n"
    "  matrix ultraqa: get the integration suite green\n\n"
    "Domain pack examples:\n"
    "  matrix executor @backend-api: add a CSV export endpoint\n"
    "  matrix review security @frontend: audit the login form\n"
    "  matrix debug @data-db: why is the user query slow\n"
    "  matrix ralph @infra-cli: make the deploy script idempotent\n\n"
    "Examples:\n"
    "  matrix review security: check auth in login.py\n"
    "  matrix executor add a CSV export endpoint\n"
    "  matrix explore: map the auth flow\n"
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
