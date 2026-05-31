"""In-memory multi-turn context store for the a2a_fleet plugin.

Process-local only — does not survive gateway restart, intentionally.
Durability (SQLite/WAL) is deferred to the async/Task phase (v0.3+).

Design notes
------------
* Uses ``threading.Lock`` for the dict — never ties state to an event loop
  (avoids the CHANGELOG:27 cross-loop trap).
* Per-contextId ``asyncio.Lock`` serialises the full read→build→append span
  for same-context concurrent calls; different contextIds stay fully concurrent.
* LRU cap on number of stored contextIds — eviction degrades to empty history
  (stateless fallback), never an error.
* Turn shape is internal only and must not leak to public interfaces.
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from collections import OrderedDict
from typing import Dict, List, Optional, TypedDict


# ---------------------------------------------------------------------------
# Internal turn shape — NOT part of the public API
# ---------------------------------------------------------------------------

class _Turn(TypedDict):
    role: str
    text: str


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class ContextStore:
    """Thread-safe, LRU-bounded in-memory context store."""

    def __init__(self, max_turns: int = 20, max_contexts: int = 500) -> None:
        self._max_turns = max_turns
        self._max_contexts = max_contexts

        # OrderedDict used as LRU: most-recently-used at end.
        # Guarded by _dict_lock (threading.Lock — never loop-bound).
        self._store: OrderedDict[str, List[_Turn]] = OrderedDict()
        self._dict_lock = threading.Lock()

        # Per-context asyncio locks — lazily created, also guarded by _dict_lock
        # on creation only.
        self._ctx_locks: Dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_context_id(self) -> str:
        """Return a fresh UUID-based context id."""
        return str(uuid.uuid4())

    def append(self, context_id: str, role: str, text: str) -> None:
        """Append a turn to the context, pruning to max_turns, touching LRU."""
        with self._dict_lock:
            self._evict_if_needed(context_id)
            if context_id not in self._store:
                self._store[context_id] = []
            self._store.move_to_end(context_id)  # touch — mark as recently used
            turns = self._store[context_id]
            turns.append({"role": role, "text": text})
            # Prune oldest turns beyond the per-context limit
            if len(turns) > self._max_turns:
                del turns[: len(turns) - self._max_turns]

    def history(
        self, context_id: str, limit: Optional[int] = None
    ) -> List[_Turn]:
        """Return a copy of the turn list for *context_id* (oldest first).

        Returns an empty list when the context is unknown or evicted.
        Does NOT touch LRU recency — call append() to touch.
        """
        with self._dict_lock:
            turns = self._store.get(context_id, [])
            result = list(turns)
        if limit is not None:
            result = result[-limit:]
        return result

    def get_lock(self, context_id: str) -> asyncio.Lock:
        """Return a per-context asyncio.Lock, creating it lazily.

        Callers hold this lock across the full read→build→append span so that
        two overlapping async calls on the same contextId are serialised.
        Different contextIds get independent locks — no global bottleneck.
        """
        with self._dict_lock:
            if context_id not in self._ctx_locks:
                self._ctx_locks[context_id] = asyncio.Lock()
            return self._ctx_locks[context_id]

    # ------------------------------------------------------------------
    # Internal helpers (called under _dict_lock)
    # ------------------------------------------------------------------

    def _evict_if_needed(self, incoming_id: str) -> None:
        """Evict the LRU context if we are at capacity and the incoming id is new."""
        if incoming_id in self._store:
            return  # already present — no eviction needed
        while len(self._store) >= self._max_contexts:
            evicted_id, _ = self._store.popitem(last=False)  # oldest
            self._ctx_locks.pop(evicted_id, None)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_store = ContextStore()


def get_store() -> ContextStore:
    """Return the process-level singleton context store."""
    return _store


def generate_context_id() -> str:
    return _store.generate_context_id()


def append(context_id: str, role: str, text: str) -> None:
    _store.append(context_id, role, text)


def history(context_id: str, limit: Optional[int] = None) -> List[_Turn]:
    return _store.history(context_id, limit)


def get_lock(context_id: str) -> asyncio.Lock:
    return _store.get_lock(context_id)
