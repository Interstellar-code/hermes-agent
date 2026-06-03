"""Persona discovery: load persona markdown from the plugin's ``personas/`` tree.

All paths resolve relative to the plugin directory (the parent of ``core/``),
so the loader works regardless of the process CWD.  Every read is defensive:
a missing file yields an empty string rather than raising, because persona
text feeds the hot path (the ``pre_llm_call`` hook) and must never crash it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

# personas/ lives next to core/, one level up from this file.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_PERSONAS_DIR = _PLUGIN_DIR / "personas"
_BASE_DIR = _PERSONAS_DIR / "_base"

# Ordered _base contracts composed into every persona.
_BASE_CONTRACTS = (
    "specialist-contract.md",
    "severity-rubric.md",
    "evidence-protocol.md",
    "boundary-table.md",
)


def _read(path: Path) -> str:
    """Return file contents, or ``""`` if missing/unreadable. Never raises."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.debug("matrix_coder: persona file not found: %s", path)
        return ""
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: failed reading %s: %s", path, exc)
        return ""


def load_base_contracts() -> List[str]:
    """Read the shared ``_base/*.md`` contracts, in composition order.

    Missing files are skipped silently (empty strings filtered out).
    """
    contents = [_read(_BASE_DIR / name) for name in _BASE_CONTRACTS]
    return [c for c in contents if c]


def load_persona(name: str) -> str:
    """Read ``personas/<name>.md`` and return its text (``""`` if missing)."""
    return _read(_PERSONAS_DIR / f"{name}.md")


def available_personas() -> List[str]:
    """List persona names (``*.md`` stems) under ``personas/``.

    Excludes the ``_base/`` directory and any dotfiles.  Returns a sorted list.
    """
    if not _PERSONAS_DIR.is_dir():
        return []
    names = [
        p.stem
        for p in _PERSONAS_DIR.glob("*.md")
        if p.is_file() and not p.name.startswith(".")
    ]
    return sorted(names)
