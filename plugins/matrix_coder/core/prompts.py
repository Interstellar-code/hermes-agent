"""Persona composition: assemble base contracts + persona into one prompt.

The composed string is what gets injected into a specialist's context (via the
child ``context`` at dispatch time) and re-asserted per turn through the
``pre_llm_call`` hook.  There is no subagent persona API — composition is pure
text.
"""

from __future__ import annotations

from typing import List, Optional

# Section markers keep the composed prompt legible and let later phases
# parse/replace individual blocks if needed.
_SEP = "\n\n" + ("-" * 60) + "\n\n"


def compose_persona(
    base_contracts: List[str],
    persona: str,
    lens: Optional[str] = None,
    domain_pack: Optional[str] = None,
) -> str:
    """Concatenate base contracts + persona (+ optional lens/domain) into one prompt.

    ``lens`` and ``domain_pack`` are active parameters: when provided, their
    content is emitted as ``# LENS`` and ``# DOMAIN PACK`` sections respectively,
    after the base contracts and the ``# PERSONA`` section.
    """
    sections: List[str] = []

    for contract in base_contracts:
        if contract and contract.strip():
            sections.append(contract.strip())

    if persona and persona.strip():
        sections.append("# PERSONA\n\n" + persona.strip())

    if lens and lens.strip():
        sections.append("# LENS\n\n" + lens.strip())

    if domain_pack and domain_pack.strip():
        sections.append("# DOMAIN PACK\n\n" + domain_pack.strip())

    return _SEP.join(sections)
