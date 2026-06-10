"""Thin adapter between Matrix Coder and the Hermes runtime + per-dispatch state.

Matrix Coder does NOT perform a real subagent dispatch (not available from
hooks); specialists are bound by text composition injected via ``pre_llm_call``.
This module provides the shared, in-memory bookkeeping the harness and the hooks
both read:

* the currently-active composed persona text (set when a trigger turn fires,
  cleared on the next non-trigger turn) — the ``pre_llm_call`` hook reads this
  to inject persona text for the current turn;
* a per-dispatch FILE-CLAIM set — the foundation of the single-writer-per-file
  guardrail.  Only bookkeeping today (claim / release / query / conflict-check);
  no hook enforces it — single-writer is advisory, enforced at orchestration
  time.

A single module-level :data:`bridge` instance is shared across the plugin so
the synchronous hooks and the harness see the same state.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Iterable, List, Optional, Set

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
        self._active_card_id: Optional[str] = None
        self._active_child_ids: List[str] = []
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

    # -- audit-mirror card bookkeeping (Phase 2) ----------------------------

    def set_active_card(self, card_id: Optional[str]) -> None:
        """Record the audit-mirror card id for the active dispatch.

        Tracked alongside the active persona so the hooks can later close the
        matching card. Setting ``None`` records "no card" (e.g. mirroring was
        disabled or card creation failed) without auto-closing anything.
        """
        with self._lock:
            self._active_card_id = card_id

    def active_card_id(self) -> Optional[str]:
        """Return the active dispatch's audit-mirror card id, or ``None``."""
        with self._lock:
            return self._active_card_id

    def clear_active_card(self) -> None:
        """Forget the active card id and all child ids WITHOUT closing them.

        Closing cards requires a kanban call — that is the hooks' job. This
        only drops the local bookkeeping so stale ids can't be reused.
        """
        with self._lock:
            self._active_card_id = None
            self._active_child_ids = []

    def register_child_card(self, child_id: str) -> None:
        """Record an open child dispatch card id for the active invocation.

        Called by the loop driver (Phase 3) after opening a child card for each
        specialist dispatch, so the hooks can later close any orphaned children.
        """
        with self._lock:
            self._active_child_ids.append(child_id)

    def pop_child_card_ids(self) -> List[str]:
        """Return and clear all open child card ids for the active invocation.

        Returns the list at the moment of the call and immediately empties the
        internal list, so a second call returns ``[]``.
        """
        with self._lock:
            ids = list(self._active_child_ids)
            self._active_child_ids = []
            return ids

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
