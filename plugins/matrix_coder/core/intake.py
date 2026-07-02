"""Invocation parsing + intake gate for Matrix Coder (Phase 1 / Phase 4).

Matrix Coder is invoked EXPLICITLY: the user's message starts with the trigger
word ``matrix`` followed by an optional role, an optional review lens, an
optional ``@domain``, an optional ``:`` separator, and the goal text. The
``pre_llm_call`` hook parses that message and, when a trigger is present,
composes the matching persona (+ optional domain pack) into the same turn.

This module owns:

* :func:`parse_trigger` — the grammar parser (returns a :class:`ParsedInvocation`
  or ``None`` when there is no trigger);
* :func:`looks_sensitive` — a cheap heuristic flagging goals that touch
  security-sensitive areas (auth, secrets, migrations, CI/deploy);
* :func:`intake_gate` — turns a parsed invocation into an
  :class:`~core.models.IntakeDecision`.

Grammar::

    matrix <role> [<lens>] [@<domain>] [:] <goal...>

* trigger word ``matrix`` (case-insensitive) ONLY when the stripped message
  starts with it;
* ``<role>`` ∈ {review, executor, explore, plan, debug, test, verify,
  simplify} OR a workflow ∈ {ralph, autopilot, ultrawork, ultraqa}. If the first
  token after the trigger is NOT a known role/workflow, the role defaults to
  ``review`` and the entire remainder becomes the goal;
* ``<lens>`` applies ONLY when ``role == review`` and the next token ∈
  {security, code, api, performance, quality, deps}; otherwise there is no lens
  (workflows never take a lens — any token after a workflow name is part of the
  goal);
* ``@<domain>`` is an OPTIONAL composable context layer token. It appears after
  the role (and after the lens for review), before the optional ``:``/goal. The
  ``<name>`` part must be in :data:`DOMAINS`; if the ``@``-prefixed token is not
  a known domain it is left in the goal unchanged. Domain applies to any role
  OR workflow, and also works with the default role (``matrix @frontend: ...``).
  A trailing ``:`` glued to the token (``@backend-api:``) is stripped before the
  domain lookup;
* an optional ``:`` separates the header from the goal and is stripped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .models import IntakeDecision, Verdict

logger = logging.getLogger(__name__)

# -- grammar constants ------------------------------------------------------

TRIGGER = "matrix"
ROLES = {
    "review",
    "executor",
    "explore",
    "plan",
    "debug",
    "test",
    "verify",
    "simplify",
}
REVIEW_LENSES = {"security", "code", "api", "performance", "quality", "deps"}
WORKFLOWS = {"ralph", "autopilot", "ultrawork", "ultraqa"}
DOMAINS = {"frontend", "backend-api", "data-db", "infra-cli", "plugin-skill-authoring"}
_DEFAULT_ROLE = "review"

# Cheap keyword/substring patterns that flag a goal as touching a
# security-sensitive area. Matched case-insensitively against the goal text.
SENSITIVE_PATH_PATTERNS = [
    "auth",
    "login",
    "password",
    "passwd",
    "secret",
    "secrets",
    "credential",
    "token",
    "api key",
    "apikey",
    "private key",
    "migration",
    "migrations",
    "security",
    ".env",
    "deploy",
    "ci/cd",
    "ci config",
    "github actions",
    "ci workflow",
    "dockerfile",
    "kubernetes",
    "k8s",
]


@dataclass
class ParsedInvocation:
    """A parsed Matrix Coder trigger.

    ``role`` is one of :data:`ROLES` or :data:`WORKFLOWS`; ``lens`` is set only
    for a review role whose header named a lens; ``domain`` is set when an
    ``@<name>`` token matching :data:`DOMAINS` appears after the role/lens and
    before the goal; ``goal`` is the remaining free-form text.
    """

    role: str
    lens: Optional[str]
    goal: str
    domain: Optional[str] = None


def parse_trigger(message: str) -> Optional[ParsedInvocation]:
    """Parse *message* into a :class:`ParsedInvocation`, or ``None``.

    Returns ``None`` when the stripped message does not START with the trigger
    word (case-insensitive). Implements the module's grammar; never raises.
    """
    try:
        if not message:
            return None
        stripped = message.strip()
        if not stripped:
            return None

        tokens = stripped.split()
        if not tokens:
            return None
        if tokens[0].lower() != TRIGGER:
            return None

        # Drop the trigger token; everything else is the body.
        rest = tokens[1:]

        # No body at all -> default role, empty goal.
        if not rest:
            return ParsedInvocation(role=_DEFAULT_ROLE, lens=None, goal="")

        role = _DEFAULT_ROLE
        lens: Optional[str] = None
        idx = 0

        # The optional ``:`` separator may be glued to the role or lens token
        # (e.g. ``matrix review security: goal``), so compare against a
        # colon-trimmed form when classifying header tokens.
        first = rest[0].lower().rstrip(":")
        if first in ROLES or first in WORKFLOWS:
            role = first
            idx = 1
            # A lens only applies to the review role; workflows never get a lens.
            if role == "review" and idx < len(rest):
                candidate = rest[idx].lower().rstrip(":")
                if candidate in REVIEW_LENSES:
                    lens = candidate
                    idx += 1
        # else: first token is not a known role or workflow -> default role,
        # whole remainder (including that token) is the goal.

        # Optional @domain token: must appear before the goal/colon separator.
        # Only consumed when the name (without leading @) is in DOMAINS; otherwise
        # the token stays in the goal unchanged.
        domain: Optional[str] = None
        if idx < len(rest):
            candidate_raw = rest[idx]
            if candidate_raw.startswith("@"):
                candidate_name = candidate_raw[1:].lower().rstrip(":")
                if candidate_name in DOMAINS:
                    domain = candidate_name
                    idx += 1

        goal_tokens = rest[idx:]
        goal = " ".join(goal_tokens).strip()

        # Strip an optional leading ``:`` header/goal separator.
        if goal.startswith(":"):
            goal = goal[1:].strip()

        return ParsedInvocation(role=role, lens=lens, goal=goal, domain=domain)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("matrix_coder: parse_trigger suppressed error: %s", exc, exc_info=True)
        return None


def looks_sensitive(goal: str) -> bool:
    """Cheap heuristic: does *goal* mention a security-sensitive area?

    Substring match (case-insensitive) against :data:`SENSITIVE_PATH_PATTERNS`.
    Never raises.
    """
    try:
        if not goal:
            return False
        low = goal.lower()
        return any(pat in low for pat in SENSITIVE_PATH_PATTERNS)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("matrix_coder: looks_sensitive suppressed error: %s", exc, exc_info=True)
        return False


def _looks_trivial(goal: str) -> bool:
    """Rough triviality signal: a short, single-clause goal.

    Used only to compute the (logged-only) ``direct_recommended`` heuristic.
    """
    g = (goal or "").strip()
    if not g:
        return False
    # Trivial ~ short and not multi-sentence.
    return len(g.split()) <= 6 and g.count(".") <= 1


def intake_gate(parsed: ParsedInvocation) -> IntakeDecision:
    """Decide the route for an EXPLICIT trigger.

    Because the user explicitly invoked Matrix Coder, the verdict is ALWAYS
    :attr:`~core.models.Verdict.MATRIX` — the user asked for it. The
    ``proposed_route`` is the role, plus the lens for a lensed review
    (``"review:security"``).

    A ``direct_recommended`` heuristic (trivial goal AND not sensitive) is
    computed and LOGGED only. The direct-recommendation / ask flow belongs to
    the Phase-5 IMPLICIT path and is intentionally NOT wired here: an explicit
    trigger is never silently downgraded to a direct answer.
    """
    if parsed.role in WORKFLOWS:
        route = parsed.role
    elif parsed.role == "review" and parsed.lens:
        route = f"{parsed.role}:{parsed.lens}"
    else:
        route = parsed.role

    if parsed.domain:
        route = f"{route}@{parsed.domain}"

    # Phase-5 preview, log-only: would we have recommended a direct answer if
    # this had arrived via the implicit path? (Never acted on in Phase 1.)
    direct_recommended = _looks_trivial(parsed.goal) and not looks_sensitive(
        parsed.goal
    )
    logger.debug(
        "matrix_coder: intake route=%s direct_recommended=%s (log-only, Phase 5)",
        route,
        direct_recommended,
    )

    return IntakeDecision(
        verdict=Verdict.MATRIX,
        reason="Explicit trigger: user invoked Matrix Coder directly.",
        proposed_route=route,
    )
