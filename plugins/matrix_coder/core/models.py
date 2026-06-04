"""Typed objects for the Matrix Coder specialist contract.

These are stdlib-only dataclasses + enums shared across the harness, the
persona composition layer, and the reporting layer.  They encode the shared
specialist output contract (Findings / Open Questions / Positive
Observations / Recommendation) and the intake-gate decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Severity(Enum):
    """Finding severity, ordered most→least serious."""

    BLOCKER = "BLOCKER"
    HIGH = "HIGH"
    MED = "MED"
    LOW = "LOW"
    NIT = "NIT"


class Verdict(Enum):
    """Intake-gate result: handle DIRECTly, or route through the MATRIX."""

    DIRECT = "DIRECT"
    MATRIX = "MATRIX"


@dataclass
class Finding:
    """A single specialist observation.

    ``location`` is a ``"file:line"`` string when known, else ``None``.
    """

    title: str
    severity: Severity
    location: Optional[str] = None
    evidence: str = ""
    recommendation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "severity": self.severity.value,
            "location": self.location,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
        }


@dataclass
class SpecialistRequest:
    """Input to a single specialist dispatch.

    ``role`` is one of the (later-phase) role names; ``lens`` narrows a review
    role (e.g. ``"security"``); ``domain`` names an optional domain pack.
    ``file_set`` is the disjoint set of files this specialist owns — the
    foundation of the single-writer-per-file guardrail.
    """

    role: str
    goal: str
    context: str = ""
    lens: Optional[str] = None
    domain: Optional[str] = None
    file_set: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "goal": self.goal,
            "context": self.context,
            "lens": self.lens,
            "domain": self.domain,
            "file_set": list(self.file_set),
        }


@dataclass
class SpecialistResult:
    """Output of a single specialist dispatch, shaped per the output contract.

    ``raw`` carries the unparsed model text when available (``None`` in the
    Phase 0 walking skeleton, which makes no LLM call).
    """

    role: str
    findings: List[Finding] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)
    positives: List[str] = field(default_factory=list)
    recommendation: str = ""
    raw: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "findings": [f.to_dict() for f in self.findings],
            "open_questions": list(self.open_questions),
            "positives": list(self.positives),
            "recommendation": self.recommendation,
            "raw": self.raw,
        }


@dataclass
class IntakeDecision:
    """Result of the intake gate: whether to route a request through the matrix.

    ``proposed_route`` is an optional human-readable hint of the role/lens
    pipeline a MATRIX verdict would take.
    """

    verdict: Verdict
    reason: str = ""
    proposed_route: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "proposed_route": self.proposed_route,
        }
