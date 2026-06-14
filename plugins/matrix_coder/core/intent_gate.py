"""Deterministic implicit-intent routing for Matrix Coder Phase 5.

The ``pre_llm_call`` hook runs on every conversation turn, so this module is
deliberately cheap and conservative: it uses local string/regex heuristics only,
never calls an LLM, and requires a coding-specific signal before routing.

Explicit ``matrix ...`` invocations are excluded here and remain owned by
``intake.parse_trigger``. This makes the precedence rule concrete: explicit
trigger parsing always wins over inferred routing.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Optional

from .intake import ParsedInvocation, looks_sensitive, parse_trigger
from .models import IntakeDecision, Verdict

logger = logging.getLogger(__name__)

_ROLE_ORDER = (
    "review",
    "verify",
    "debug",
    "test",
    "simplify",
    "explore",
    "plan",
    "executor",
)

_CODING_TERMS = (
    "api",
    "auth",
    "backend",
    "bug",
    "ci",
    "class",
    "cli",
    "code",
    "component",
    "config",
    "css",
    "database",
    "dependency",
    "deploy",
    "docker",
    "endpoint",
    "error",
    "exception",
    "frontend",
    "function",
    "github actions",
    "implementation",
    "kubernetes",
    "library",
    "migration",
    "module",
    "package",
    "plugin",
    "pr",
    "pull request",
    "query",
    "react",
    "readme",
    "codebase",
    "repo",
    "repository",
    "schema",
    "script",
    "sdk",
    "skill.md",
    "sql",
    "stack trace",
    "test",
    "typescript",
    "ui",
    "docs",
    "documentation",
)

_ACTION_TERMS = (
    "add",
    "audit",
    "build",
    "change",
    "check",
    "clean up",
    "create",
    "debug",
    "design",
    "fix",
    "implement",
    "inspect",
    "map",
    "optimize",
    "plan",
    "refactor",
    "remove",
    "review",
    "simplify",
    "trace",
    "update",
    "verify",
)

_ROLE_SIGNALS = {
    "review": (
        "review",
        "audit",
        "is this safe",
        "security hole",
        "vulnerability",
        "code quality",
        "performance issue",
        "backward compatible",
    ),
    "verify": ("verify", "confirm", "prove", "did the fix work", "does the fix work"),
    "debug": (
        "debug",
        "root cause",
        "why does",
        "why is",
        "error",
        "exception",
        "crash",
        "failing",
        "fails",
        "hang",
        "broken",
    ),
    "test": ("add tests", "write tests", "create tests", "test coverage", "strengthen tests"),
    "simplify": ("simplify", "refactor", "clean up", "cleanup", "overengineered"),
    "explore": ("explore", "map", "trace", "where is", "where does", "understand"),
    "plan": ("plan", "design", "architect", "break down"),
    "executor": ("implement", "add", "build", "fix", "change", "update", "remove"),
}

_LENS_SIGNALS = {
    "security": (
        "security",
        "safe",
        "auth",
        "login",
        "password",
        "secret",
        "credential",
        "token",
        "permission",
        "access control",
        "vulnerability",
        "injection",
        "oauth",
    ),
    "performance": (
        "performance",
        "slow",
        "latency",
        "n+1",
        "memory leak",
        "cpu",
        "optimize",
    ),
    "deps": ("dependency", "dependencies", "package", "library", "sdk", "cve", "license"),
    "api": ("api", "endpoint", "contract", "backward compatible", "schema"),
    "quality": ("quality", "maintainability", "solid", "anti-pattern", "code smell"),
}

_DOMAIN_SIGNALS = {
    "plugin-skill-authoring": ("plugin", "skill.md", "skill author"),
    "frontend": ("frontend", "react", "component", "css", "ui", "browser", "accessibility"),
    "data-db": ("database", "db", "sql", "query", "migration", "schema", "index"),
    "infra-cli": (
        "cli",
        "terminal",
        "shell",
        "docker",
        "kubernetes",
        "k8s",
        "deploy",
        "ci",
        "github actions",
    ),
    "backend-api": ("backend", "api", "endpoint", "route", "http", "server"),
}

_MECHANICAL_DIRECT_SIGNALS = (
    "typo",
    "spelling",
    "readme",
    "docs",
    "documentation",
    "comment",
    "rename",
    "formatting",
    "format ",
    "copy text",
)

_DIRECT_ACCEPTANCE_SIGNALS = (
    "handle it directly",
    "handle this directly",
    "do it directly",
    "let hermes do it",
    "let hermes handle",
)

_EXPLICIT_TECHNICAL_TERMS = _CODING_TERMS + (
    "security",
    "performance",
    "quality",
    "worker",
    "refactor",
    "coverage",
    "runtime",
    "lint",
    "typecheck",
    "the fix",
)

# ---------------------------------------------------------------------------
# Negative-signal short-circuits: advisory / meta / addressee signals that
# indicate the user is asking Hermes a question, not commissioning coding work.
# ---------------------------------------------------------------------------

_ADVISORY_META_SIGNALS = (
    "should i",
    "should we",
    "do you think",
    "what do you think",
    "what is the best",
    "what's the best",
    "can you explain",
    "tell me about",
    "is it ready",
    "ready for",
    "up to date",
    "what do you recommend",
    "give me pointers",
    "pointers on",
)

_ADDRESSEE_SIGNALS = ("hermes",)  # leading addressee to the orchestrator

# Addressee prefix regex: matches "hermes" at the start of a message when
# followed by a word boundary + separator (space, comma, colon).
# This prevents suppression of "hermespkg needs a refactor" or "hermesutils.py has a bug".
_ADDRESSEE_PREFIX_RE = re.compile(r"^hermes\b[\s,:]", re.IGNORECASE)

_BARE_LOCATE_SIGNALS = ("where is", "where are")

# ---------------------------------------------------------------------------
# Flat tuple of distinctive verbs/phrases from _ROLE_SIGNALS.
# Used in has_coding_intent to require a real action signal alongside a coding
# noun — so a bare "?" or wh-prefix is no longer sufficient.
#
# NOTE: Keep role-name words (review, debug, plan, simplify, build, etc.) OUT
# of this tuple — they appear in _ACTION_TERMS and cause the "action+role_verb
# co-occurrence" fast path to fire on innocent sentences like "plan a weekend
# trip" or "debug my relationship".  Only include diagnostic/symptom signals
# that are NOT themselves role-name words.
# ---------------------------------------------------------------------------

_ROLE_SIGNAL_VERBS = (
    "audit",
    "is this safe",
    "security hole",
    "vulnerability",
    "code quality",
    "performance issue",
    "backward compatible",
    "confirm",
    "prove",
    "did the fix work",
    "does the fix work",
    "root cause",
    "why does",
    "why is",
    "crash",
    "failing",
    "fails",
    "hang",
    "broken",
    "add tests",
    "write tests",
    "create tests",
    "test coverage",
    "strengthen tests",
    "refactor",
    "clean up",
    "cleanup",
    "overengineered",
    "trace",
    "where does",
    "architect",
    "break down",
    "optimize",
)

# Unambiguous coding-task signals — a subset of _ROLE_SIGNAL_VERBS used for
# the "action + coding-task-signal" co-occurrence fast path in has_coding_intent
# and _is_strong_signal.  These phrases cannot plausibly appear in non-coding
# sentences, so they fire as strong signals even without a coding noun.
_FAILURE_SIGNALS = (
    # diagnostic / symptom signals
    "crash",
    "failing",
    "fails",
    "hang",
    "broken",
    "root cause",
    "why does",
    "why is",
    "security hole",
    "vulnerability",
    "overengineered",
    # unambiguous test-authoring phrases (plural "tests" avoids word-boundary miss)
    "add tests",
    "write tests",
    "create tests",
    "test coverage",
    "strengthen tests",
)

_EXPLICIT_ROLE_RE = re.compile(
    r"^(?:please\s+)?(review|executor|explore|plan|debug|test|verify|simplify)\b",
    re.IGNORECASE,
)

_PATH_OR_EXTENSION_RE = re.compile(
    r"(?:^|\s)(?:[\w.-]+/)+[\w.-]+|"
    r"\b[\w-]+\.(?:py|pyi|js|jsx|ts|tsx|go|rs|java|rb|php|cs|cpp|c|h|"
    r"yaml|yml|toml|json|md|sql|sh)\b",
    re.IGNORECASE,
)


@lru_cache(maxsize=None)
def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(_term_pattern(term).search(text) for term in terms)


def has_coding_intent(message: str) -> bool:
    """Return whether *message* has enough coding signal for implicit routing.

    Negative-signal short-circuits fire first so advisory/meta/locate questions
    addressed to Hermes are never silently handed to the specialist:
    - Advisory / meta signals ("should I", "do you think", etc.) → False
    - Leading orchestrator addressee ("hermes, ...") → False
    - Bare locate questions ("where is X") with no action/role imperative → False

    After short-circuits, a coding noun alone plus bare "?" or a wh-prefix is
    no longer sufficient — a real action signal or role-signal verb must also be
    present.
    """
    try:
        low = (message or "").strip().lower()
        if not low:
            return False
        # --- Negative-signal short-circuits ---
        # Advisory/meta signals only suppress when they LEAD the message.
        # A trailing advisory clause (e.g. "fix the bug, should i mention it
        # also breaks login") must not suppress the imperative at the start.
        if any(low.startswith(sig) for sig in _ADVISORY_META_SIGNALS):
            return False
        # Addressee prefix requires a word boundary + separator so that
        # "hermespkg needs a refactor" or "hermesutils.py has a bug" is NOT
        # suppressed — only "hermes, ..." / "hermes: ..." / "hermes what..." are.
        if _ADDRESSEE_PREFIX_RE.match(low):
            return False
        if _contains_any(low, _BARE_LOCATE_SIGNALS) and not (
            _contains_any(low, _ACTION_TERMS) or _contains_any(low, _ROLE_SIGNAL_VERBS)
        ):
            return False
        # --- Positive-signal fast paths ---
        if _PATH_OR_EXTENSION_RE.search(low):
            return True
        explicit_role = _EXPLICIT_ROLE_RE.match(low)
        if explicit_role and _contains_any(
            low[explicit_role.end() :], _EXPLICIT_TECHNICAL_TERMS
        ):
            return True
        # Strong co-occurrence: action verb + failure/symptom signal is sufficient
        # even without a coding noun (e.g. "fix the login crash").
        # Use _FAILURE_SIGNALS (not all of _ROLE_SIGNAL_VERBS) to avoid false
        # positives where a role-name word is both the action and the signal
        # (e.g. "plan a weekend trip", "debug my relationship").
        if _contains_any(low, _ACTION_TERMS) and _contains_any(low, _FAILURE_SIGNALS):
            return True
        # Coding noun required; bare "?" or wh-prefix alone is NOT sufficient —
        # a real action term or role-signal verb must also be present.
        return _contains_any(low, _CODING_TERMS) and (
            _contains_any(low, _ACTION_TERMS)
            or _contains_any(low, _ROLE_SIGNAL_VERBS)
            or bool(_PATH_OR_EXTENSION_RE.search(low))
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("matrix_coder: has_coding_intent suppressed error: %s", exc, exc_info=True)
        return False


def _infer_role(low: str) -> str:
    explicit = _EXPLICIT_ROLE_RE.match(low)
    if explicit:
        return explicit.group(1)
    for role in _ROLE_ORDER:
        if _contains_any(low, _ROLE_SIGNALS[role]):
            return role
    return "review"


def _infer_lens(low: str, role: str) -> Optional[str]:
    if role != "review":
        return None
    for lens in ("security", "performance", "deps", "api", "quality"):
        if _contains_any(low, _LENS_SIGNALS[lens]):
            return lens
    return "code"


def _infer_domain(low: str) -> Optional[str]:
    for domain in (
        "plugin-skill-authoring",
        "frontend",
        "data-db",
        "infra-cli",
        "backend-api",
    ):
        if _contains_any(low, _DOMAIN_SIGNALS[domain]):
            return domain
    return None


def infer_implicit_invocation(message: str) -> Optional[ParsedInvocation]:
    """Infer role/lens/domain from a plain coding request, or return ``None``.

    The function excludes explicit ``matrix ...`` triggers so callers can
    guarantee the parse order ``explicit -> implicit -> no-op``.
    """
    try:
        low = (message or "").strip().lower()
        if (
            parse_trigger(message) is not None
            or _contains_any(low, _DIRECT_ACCEPTANCE_SIGNALS)
            or not has_coding_intent(message)
        ):
            return None
        goal = (message or "").strip()
        role = _infer_role(low)
        return ParsedInvocation(
            role=role,
            lens=_infer_lens(low, role),
            goal=goal,
            domain=_infer_domain(low),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "matrix_coder: infer_implicit_invocation suppressed error: %s", exc, exc_info=True
        )
        return None


def _route_name(parsed: ParsedInvocation) -> str:
    route = parsed.role
    if parsed.role == "review" and parsed.lens:
        route = f"{route}:{parsed.lens}"
    if parsed.domain:
        route = f"{route}@{parsed.domain}"
    return route


def _is_strong_signal(low: str) -> bool:
    """Return True only when the request is a genuine specialist coding task.

    Strong signals:
    - Explicit role-prefix lead (matches _EXPLICIT_ROLE_RE), e.g. "review auth"
    - Imperative action on code: an _ACTION_TERMS verb co-occurring with a
      coding noun or path token, i.e. unambiguous "do X to the code" phrasing.
    - Diagnostic/role-signal verb (crash, failing, why does, etc.) co-occurring
      with a coding noun — covers "why does the API crash?" debug patterns.
    - Action + role-signal verb co-occurrence (already caught by has_coding_intent
      but also strong enough to skip the ask: "fix the login crash").

    Anything advisory or purely interrogative (bare "?" on a coding noun, etc.)
    that slipped past has_coding_intent is weak/ambiguous and routes to ask.
    """
    if _EXPLICIT_ROLE_RE.match(low):
        return True
    has_coding_noun = _contains_any(low, _CODING_TERMS) or bool(_PATH_OR_EXTENSION_RE.search(low))
    # MED-1 guard: a purely interrogative message (ends with "?", no failure
    # signal) does NOT count as a strong imperative even when an action term
    # like "fix" appears as a NOUN ("did the fix work?").  Only suppress the
    # strong-signal for this branch — failure signals still override.
    _purely_q = low.rstrip().endswith("?") and not _contains_any(low, _FAILURE_SIGNALS)
    if _contains_any(low, _ACTION_TERMS) and has_coding_noun and not _purely_q:
        return True
    # action + failure/symptom signal (e.g. "fix the login crash") is strong
    # even without a coding noun — and overrides the purely-interrogative guard.
    if _contains_any(low, _ACTION_TERMS) and _contains_any(low, _FAILURE_SIGNALS):
        return True
    # Diagnostic role-signal verb + coding noun (e.g. "why does the API crash?")
    # EXCEPTION (MED-1): purely interrogative messages ("is this api backward
    # compatible?", "did the fix work on the auth module?") are evaluative
    # questions to the orchestrator — not specialist work.  They fall through to
    # the weak/ask path.  A message is "purely interrogative" when:
    #   * it ends with "?" AND
    #   * contains NO _ACTION_TERMS imperative AND
    #   * contains NO _FAILURE_SIGNALS (e.g. "crash", "failing").
    # "why does the API endpoint crash?" still routes MATRIX because it has a
    # _FAILURE_SIGNAL ("crash").
    if _contains_any(low, _ROLE_SIGNAL_VERBS) and has_coding_noun:
        # MED-1: purely interrogative evaluative questions ("is this api
        # backward compatible?", "did the fix work on the auth module?") are
        # directed at the orchestrator, not the specialist.  They fall through
        # to the weak/ask path.  The sole exception: failure signals ("crash",
        # "failing", etc.) override, so "why does the API endpoint crash?" is
        # still MATRIX.  We do NOT check _ACTION_TERMS here because action words
        # like "fix" frequently appear as nouns in yes/no questions.
        purely_interrogative = (
            low.rstrip().endswith("?")
            and not _contains_any(low, _FAILURE_SIGNALS)
        )
        if not purely_interrogative:
            return True
    return False


def implicit_intake_gate(parsed: ParsedInvocation) -> IntakeDecision:
    """Right-size an inferred request before implicit specialist activation.

    Policy (new in fix/matrix-coder-140-invocation):
    - Mechanical DIRECT branch: unchanged — short, mechanical, low-risk.
    - Strong signal: explicit role-prefix OR imperative action on code →
      MATRIX (silent auto-activate), same as before.
    - Weak/ambiguous signal (coding noun + question phrasing, etc.) →
      DIRECT verdict with visible ask so the user can confirm.  We reuse
      Verdict.DIRECT + direct_recommendation_context to avoid adding a new
      enum value and touching harness.py; the reason field carries the
      proposed route so the prompt surfaces it.
    """
    low = (parsed.goal or "").lower()
    mechanical = _contains_any(low, _MECHANICAL_DIRECT_SIGNALS)
    short = 0 < len(low.split()) <= 10 and low.count(".") <= 1
    direct = (
        parsed.role in {"executor", "simplify"}
        and mechanical
        and short
        and not looks_sensitive(parsed.goal)
    )
    if direct:
        return IntakeDecision(
            verdict=Verdict.DIRECT,
            reason="Clear, mechanical, low-risk request; specialist overhead is unlikely to help.",
        )
    route = _route_name(parsed)
    if _is_strong_signal(low):
        return IntakeDecision(
            verdict=Verdict.MATRIX,
            reason="Coding request benefits from a focused specialist persona.",
            proposed_route=route,
        )
    # Weak/ambiguous — surface a visible ask instead of silently activating.
    return IntakeDecision(
        verdict=Verdict.DIRECT,
        reason=(
            f"Inferred specialist route '{route}' but signal is ambiguous — "
            "asking for confirmation before activating."
        ),
        proposed_route=route,
    )


def direct_recommendation_context(decision: IntakeDecision) -> str:
    """Return ephemeral instructions that visibly offer the DIRECT verdict.

    Works for two cases:
    1. Mechanical/low-risk: recommend Hermes handles it directly.
    2. Ambiguous inferred specialist (proposed_route set): ask the user whether
       they want to activate the inferred specialist or let Hermes handle it.
    """
    if decision.proposed_route:
        specialist_hint = (
            f' (inferred route: <b>{decision.proposed_route}</b>)'
        )
    else:
        specialist_hint = ""
    return (
        "<matrix_coder_intake>\n"
        "  <verdict>direct</verdict>\n"
        f"  <reason>{decision.reason}</reason>\n"
        "  <instruction>Before acting, surface this recommendation to the user: "
        f'"Efficient for Hermes to handle this directly{specialist_hint}. '
        'Invoke Matrix Coder anyway, or let Hermes do it?" If they choose Matrix, '
        "have them repeat the request prefixed with `matrix`; `handle directly` "
        "must proceed without repeating this recommendation. "
        "Do not claim a Matrix specialist is active.</instruction>\n"
        "</matrix_coder_intake>"
    )
