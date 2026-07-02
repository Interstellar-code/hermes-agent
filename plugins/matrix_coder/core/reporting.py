"""Render a :class:`SpecialistResult` per the shared output contract.

The contract has four sections, always in this order:

    Findings / Open Questions / Positive Observations / Recommendation

:func:`render_markdown` produces the human-facing text; :func:`render_json`
produces a machine-readable dict for tooling/dashboards.
"""

from __future__ import annotations

from typing import Any, Dict

from .models import SpecialistResult


def render_markdown(result: SpecialistResult) -> str:
    """Render *result* as markdown following the shared output contract."""
    lines = [f"# Matrix Coder — {result.role}", ""]

    # Findings
    lines.append("## Findings")
    if result.findings:
        for f in result.findings:
            loc = f" (`{f.location}`)" if f.location else ""
            lines.append(f"- **[{f.severity.value}] {f.title}**{loc}")
            if f.evidence:
                lines.append(f"  - Evidence: {f.evidence}")
            if f.recommendation:
                lines.append(f"  - Recommendation: {f.recommendation}")
    else:
        lines.append("- None.")
    lines.append("")

    # Open Questions
    lines.append("## Open Questions")
    if result.open_questions:
        lines.extend(f"- {q}" for q in result.open_questions)
    else:
        lines.append("- None.")
    lines.append("")

    # Positive Observations
    lines.append("## Positive Observations")
    if result.positives:
        lines.extend(f"- {p}" for p in result.positives)
    else:
        lines.append("- None.")
    lines.append("")

    # Recommendation
    lines.append("## Recommendation")
    lines.append(result.recommendation or "- None.")
    lines.append("")

    return "\n".join(lines)


def render_json(result: SpecialistResult) -> Dict[str, Any]:
    """Render *result* as a JSON-serializable dict (the output contract)."""
    return {
        "role": result.role,
        "findings": [f.to_dict() for f in result.findings],
        "open_questions": list(result.open_questions),
        "positive_observations": list(result.positives),
        "recommendation": result.recommendation,
    }
