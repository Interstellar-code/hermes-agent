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
    """Return whether *message* has enough coding signal for implicit routing."""
    try:
        low = (message or "").strip().lower()
        if not low:
            return False
        if _PATH_OR_EXTENSION_RE.search(low):
            return True
        explicit_role = _EXPLICIT_ROLE_RE.match(low)
        if explicit_role and _contains_any(
            low[explicit_role.end() :], _EXPLICIT_TECHNICAL_TERMS
        ):
            return True
        return _contains_any(low, _CODING_TERMS) and (
            _contains_any(low, _ACTION_TERMS)
            or "?" in low
            or low.startswith(("is ", "does ", "why ", "where ", "how "))
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: has_coding_intent suppressed error: %s", exc)
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
        logger.debug(
            "matrix_coder: infer_implicit_invocation suppressed error: %s", exc
        )
        return None


def _route_name(parsed: ParsedInvocation) -> str:
    route = parsed.role
    if parsed.role == "review" and parsed.lens:
        route = f"{route}:{parsed.lens}"
    if parsed.domain:
        route = f"{route}@{parsed.domain}"
    return route


def implicit_intake_gate(parsed: ParsedInvocation) -> IntakeDecision:
    """Right-size an inferred request before implicit specialist activation."""
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
    return IntakeDecision(
        verdict=Verdict.MATRIX,
        reason="Coding request benefits from a focused specialist persona.",
        proposed_route=_route_name(parsed),
    )


def direct_recommendation_context(decision: IntakeDecision) -> str:
    """Return ephemeral instructions that visibly offer the DIRECT verdict."""
    return (
        "<matrix_coder_intake>\n"
        "  <verdict>direct</verdict>\n"
        f"  <reason>{decision.reason}</reason>\n"
        "  <instruction>Before acting, surface this recommendation to the user: "
        '"Efficient for Hermes to handle this directly. Invoke Matrix Coder anyway, '
        'or let Hermes do it?" If they choose Matrix, have them repeat the request '
        "prefixed with `matrix`; `handle directly` must proceed without repeating "
        "this recommendation. Do not claim a Matrix specialist is active.</instruction>\n"
        "</matrix_coder_intake>"
    )
