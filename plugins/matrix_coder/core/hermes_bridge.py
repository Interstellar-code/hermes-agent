"""Thin adapter between Matrix Coder and the Hermes runtime + per-dispatch state.

Phase 0 does NOT perform a real subagent dispatch.  This module provides the
shared, in-memory bookkeeping the harness and the hooks both read:

* the currently-active composed persona text (set when a dispatch starts,
  cleared when it ends) — the ``pre_llm_call`` hook reads this to inject
  persona text per turn;
* a per-dispatch FILE-CLAIM set — the foundation of the single-writer-per-file
  guardrail.  Phase 0 only does the bookkeeping (claim / release / query /
  conflict-check); no hook enforces it yet.

A single module-level :data:`bridge` instance is shared across the plugin so
the synchronous hooks and the harness see the same state.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Iterable, Optional, Set

logger = logging.getLogger(__name__)


def _normalize(path: str) -> str:
    """Canonicalise a path for claim comparison.

    Phase 0 does ``expanduser`` only — it does NOT ``resolve()`` symlinks or
    ``..`` segments, so two spellings of the same file (relative vs absolute,
    or ``a/../b``) will not collide-detect. Adequate for the advisory Phase-0
    bookkeeping; a later phase tightens this when the guardrail is enforced.
    """
    try:
        return str(Path(path).expanduser())
    except Exception:  # pragma: no cover - defensive
        return path


class HermesBridge:
    """Shared, thread-safe per-dispatch state for Matrix Coder.

    Holds at most one active dispatch's composed persona at a time (Phase 0 is
    single-dispatch).  Later phases may key this by dispatch id.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_persona: Optional[str] = None
        self._claimed: Set[str] = set()

    # -- active dispatch / persona injection --------------------------------

    def set_active_persona(self, composed: str) -> None:
        """Mark a dispatch active with its composed persona text."""
        with self._lock:
            self._active_persona = composed

    def clear_active_persona(self) -> None:
        """Clear the active dispatch's persona (dispatch finished/aborted)."""
        with self._lock:
            self._active_persona = None

    def inject_persona_text(self) -> Optional[str]:
        """Return the active composed persona, or ``None`` if no dispatch is active.

        Used by the ``pre_llm_call`` hook.  Must never raise.
        """
        with self._lock:
            return self._active_persona

    def is_active(self) -> bool:
        with self._lock:
            return self._active_persona is not None

    # -- single-writer-per-file bookkeeping ---------------------------------

    def claim_files(self, paths: Iterable[str]) -> None:
        """Add *paths* to the current dispatch's claimed-file set."""
        norm = {_normalize(p) for p in paths if p}
        with self._lock:
            self._claimed |= norm

    def release_files(self) -> None:
        """Drop all file claims (dispatch finished)."""
        with self._lock:
            self._claimed.clear()

    def claimed_files(self) -> Set[str]:
        """Return a copy of the currently-claimed file set."""
        with self._lock:
            return set(self._claimed)

    def would_conflict(self, path: str) -> bool:
        """Return True if *path* is already claimed by the active dispatch.

        The single-writer-per-file guardrail: a second writer for an
        already-claimed file would conflict.  Advisory in Phase 0 (no hook
        enforces it).
        """
        target = _normalize(path)
        with self._lock:
            return target in self._claimed


# Shared instance — hooks and harness import this same object.
bridge = HermesBridge()
