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

PL-1 (session-keyed state): the per-dispatch mutable state
(``_active_persona``, ``_active_card_id``, ``_active_child_ids``) is now keyed
by ``session_id``.  All public methods accept an optional ``session_id``
argument; ``None`` maps to the sentinel key ``""`` so legacy call sites that do
not yet pass a session_id keep working unchanged.  The file-claim set
(``_claimed``) remains global — it is a write-guard advisory and is not
session-scoped.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# Sentinel key used when session_id is None — preserves backward compatibility
# for call sites that don't yet pass a session_id.
_DEFAULT_SESSION = ""


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


def _key(session_id: Optional[str]) -> str:
    """Map an optional session_id to a non-None dict key.

    A real (truthy) session_id gets its OWN slot, so concurrent sessions cannot
    bleed persona/card state into each other (PL-1). ``None``/empty collapses to
    the sentinel ``""`` — used by the ``/matrix`` smoke path and tests that don't
    thread a session_id. Production hooks (``_inject_persona`` and the harness)
    pass the real per-turn session_id consistently, so each session is isolated.
    """
    return session_id if session_id else _DEFAULT_SESSION


@dataclass
class _SessionState:
    """Per-session mutable dispatch state."""

    active_persona: Optional[str] = None
    active_card_id: Optional[str] = None
    active_child_ids: List[str] = field(default_factory=list)


class HermesBridge:
    """Shared, thread-safe per-dispatch state for Matrix Coder.

    Per-dispatch state (persona, card_id, child_ids) is keyed by session_id
    so concurrent sessions cannot bleed into each other.  Callers pass
    ``session_id=None`` (or omit it) to use the sentinel key ``""``; existing
    tests that call these methods without a session_id continue to work.

    The file-claim set remains global (not session-scoped) — it is an advisory
    bookkeeping layer that does not need per-session isolation.
    """

    # Maximum number of concurrent sessions tracked in memory (MED-2 LRU cap).
    # When this is exceeded, the oldest entry is evicted (it is abandoned).
    _MAX_SESSIONS = 256

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # OrderedDict for LRU-style eviction: newest entries are moved to the
        # end on access; when len > _MAX_SESSIONS we pop the oldest (first) key.
        self._sessions: OrderedDict[str, _SessionState] = OrderedDict()
        self._claimed: Set[str] = set()

    def _get_session(self, session_id: Optional[str]) -> _SessionState:
        """Return (creating if needed) the _SessionState for *session_id*.

        Must be called with self._lock held.

        MED-2: if the session is NEW and the dict has reached _MAX_SESSIONS,
        evict the oldest entry (the first key in the OrderedDict) before
        inserting.  Live sessions are moved to the end on every access so they
        stay recent and are not evicted while still in use.
        """
        k = _key(session_id)
        if k in self._sessions:
            # Move to end to mark as recently-used (LRU touch).
            self._sessions.move_to_end(k)
        else:
            if len(self._sessions) >= self._MAX_SESSIONS:
                # Evict oldest entry (first in OrderedDict).
                self._sessions.popitem(last=False)
            self._sessions[k] = _SessionState()
        return self._sessions[k]

    def _cleanup_session(self, session_id: Optional[str]) -> None:
        """Remove a session entry if it is fully cleared (to avoid leaking keys).

        Must be called with self._lock held.
        """
        k = _key(session_id)
        s = self._sessions.get(k)
        if s is not None and s.active_persona is None and s.active_card_id is None and not s.active_child_ids:
            self._sessions.pop(k, None)

    # -- active dispatch / persona injection --------------------------------

    def set_active_persona(self, composed: str, session_id: Optional[str] = None) -> None:
        """Mark a dispatch active with its composed persona text."""
        with self._lock:
            self._get_session(session_id).active_persona = composed

    def clear_active_persona(self, session_id: Optional[str] = None) -> None:
        """Clear the active dispatch's persona (dispatch finished/aborted)."""
        with self._lock:
            self._get_session(session_id).active_persona = None
            self._cleanup_session(session_id)

    def inject_persona_text(self, session_id: Optional[str] = None) -> Optional[str]:
        """Return the active composed persona, or ``None`` if no dispatch is active.

        Used by the ``pre_llm_call`` hook.  Must never raise.
        """
        with self._lock:
            return self._get_session(session_id).active_persona

    def is_active(self, session_id: Optional[str] = None) -> bool:
        with self._lock:
            return self._get_session(session_id).active_persona is not None

    # -- audit-mirror card bookkeeping (Phase 2) ----------------------------

    def set_active_card(self, card_id: Optional[str], session_id: Optional[str] = None) -> None:
        """Record the audit-mirror card id for the active dispatch.

        Tracked alongside the active persona so the hooks can later close the
        matching card. Setting ``None`` records "no card" (e.g. mirroring was
        disabled or card creation failed) without auto-closing anything.
        """
        with self._lock:
            self._get_session(session_id).active_card_id = card_id
            if card_id is None:
                self._cleanup_session(session_id)

    def active_card_id(self, session_id: Optional[str] = None) -> Optional[str]:
        """Return the active dispatch's audit-mirror card id, or ``None``."""
        with self._lock:
            return self._get_session(session_id).active_card_id

    def take_active_card(self, session_id: Optional[str] = None) -> Optional[str]:
        """Atomically read AND clear the active card id under a single lock.

        HIGH-2: prevents a double-close race where two concurrent callers both
        read the same card_id and both attempt to close it.  The first caller
        gets the card_id and the second gets ``None`` (no-op).  Also clears the
        child_ids list so no stale children linger.

        Returns the card id (if any) that was active before clearing.
        """
        with self._lock:
            s = self._get_session(session_id)
            card_id = s.active_card_id
            s.active_card_id = None
            s.active_child_ids = []
            self._cleanup_session(session_id)
            return card_id

    def clear_active_card(self, session_id: Optional[str] = None) -> None:
        """Forget the active card id and all child ids WITHOUT closing them.

        Closing cards requires a kanban call — that is the hooks' job. This
        only drops the local bookkeeping so stale ids can't be reused.
        """
        with self._lock:
            s = self._get_session(session_id)
            s.active_card_id = None
            s.active_child_ids = []
            self._cleanup_session(session_id)

    def register_child_card(self, child_id: str, session_id: Optional[str] = None) -> None:
        """Record an open child dispatch card id for the active invocation.

        Called by the loop driver (Phase 3) after opening a child card for each
        specialist dispatch, so the hooks can later close any orphaned children.
        """
        with self._lock:
            self._get_session(session_id).active_child_ids.append(child_id)

    def pop_child_card_ids(self, session_id: Optional[str] = None) -> List[str]:
        """Return and clear all open child card ids for the active invocation.

        Returns the list at the moment of the call and immediately empties the
        internal list, so a second call returns ``[]``.
        """
        with self._lock:
            s = self._get_session(session_id)
            ids = list(s.active_child_ids)
            s.active_child_ids = []
            self._cleanup_session(session_id)
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
