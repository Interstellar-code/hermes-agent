"""Per-session deferred-tool pool for lazy MCP loading.

Each conversation session gets its own ``DeferredToolPool`` keyed by
``agent.session_id``. The pool tracks which MCP tools have been
promoted (loaded with full schemas) within that session, so a
subsequent turn knows to keep them visible as full tools rather than
collapsing them back into stubs.

Cross-session isolation is the whole point: session A promoting
``mcp_trek_search`` must NOT cause session B to also see the full
schema. The pool registry keys on session_id and uses a weak-value
dictionary so finished sessions get garbage-collected naturally.

Session-end events (``/new`` and its ``/reset`` alias both route to
``new_session()`` in ``hermes_cli/cli.py``) fire a ``pre_session_reset``
hook that calls :func:`evict` explicitly — belt for the GC suspenders.
"""
from __future__ import annotations

import contextvars
import logging
import threading
import weakref
from typing import Dict, Optional, Set

# Set by ``hook_impl.transform_tools`` on every request, so downstream
# code paths (notably the ``load_mcp_tools`` meta-tool handler) can
# resolve the active agent's session without the tool registry having
# to plumb agent references through ``registry.dispatch``.
_current_agent_var: "contextvars.ContextVar[Optional[object]]" = contextvars.ContextVar(
    "mcp_lazy_current_agent", default=None,
)

logger = logging.getLogger(__name__)


class DeferredToolPool:
    """Per-session state for promoted MCP tools.

    The pool only remembers *names* of promoted tools — the schemas
    themselves come from ``get_tool_definitions()`` at request build
    time, so a session's promoted set survives MCP-server hot-reloads
    without holding stale schema copies.
    """

    # ``__weakref__`` is required so WeakValueDictionary can hold us.
    __slots__ = (
        "session_id", "_promoted", "_promoted_servers", "_prev_mode",
        "_turn", "_last_used", "_lazy_active", "_lock", "__weakref__",
    )

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._promoted: Set[str] = set()
        self._promoted_servers: Set[str] = set()
        # Tracks the last-seen discovery_mode for this session so hook_impl
        # can detect mid-session flips without a module-level dict that leaks
        # across sessions.  Stored here so evict() clears it automatically.
        # Fixes Interstellar-code/hermes-agent#29.
        self._prev_mode: Optional[str] = None
        # Request counter + per-tool last-use stamps for idle eviction.
        # "turn" = one transform_tools call = one LLM request.
        self._turn: int = 0
        self._last_used: Dict[str, int] = {}
        # False when transform_tools decided to pass through (auto mode,
        # below threshold) — tells pre_tool_call not to intercept calls
        # this turn, since full schemas are in the prompt.
        self._lazy_active: bool = True
        self._lock = threading.RLock()

    def promote(self, names) -> None:
        """Mark one or more tool names as promoted in this session.

        Accepts a string or any iterable of strings. Whitespace-only
        and empty entries are silently dropped — defensive against
        the model passing junk.
        """
        if isinstance(names, str):
            names = (names,)
        with self._lock:
            for n in names:
                if isinstance(n, str) and n.strip():
                    n = n.strip()
                    self._promoted.add(n)
                    self._last_used[n] = self._turn

    def tick(self) -> int:
        """Advance and return the per-session request counter."""
        with self._lock:
            self._turn += 1
            return self._turn

    def touch(self, name: str) -> None:
        """Stamp ``name`` as used this turn (call on promoted-tool dispatch)."""
        with self._lock:
            if name in self._promoted:
                self._last_used[name] = self._turn

    def evict_idle(self, idle_turns: int) -> list:
        """Demote promoted tools unused for ``idle_turns`` requests.

        Batched by design: the caller gates this on total promoted-schema
        cost so the tool list (and provider prompt-cache prefix) churns
        once per batch, not once per tool. Returns evicted names.
        """
        if idle_turns <= 0:
            return []
        with self._lock:
            evicted = [
                n for n in self._promoted
                if self._turn - self._last_used.get(n, 0) >= idle_turns
            ]
            for n in evicted:
                self._promoted.discard(n)
                self._last_used.pop(n, None)
            return evicted

    def snapshot(self) -> frozenset:
        """Immutable view of currently-promoted names.

        Returns a ``frozenset`` so callers can pass it across thread
        boundaries (e.g. into the tool-list builder) without worrying
        about mid-iteration mutation.
        """
        with self._lock:
            return frozenset(self._promoted)

    def is_promoted(self, name: str) -> bool:
        with self._lock:
            return name in self._promoted

    def promote_server(self, name: str, eager: bool = False) -> None:
        """Mark a server as promoted in this session.

        Idempotent.  The ``eager`` flag is accepted for API compatibility but
        is NOT stored here — eager-vs-stub behaviour is handled at call time in
        ``promote.promote_server_tools`` by directly promoting individual tools
        into ``_promoted`` when eager=True.  Storing it here was a no-op and
        produced a spurious WARNING on benign idempotent re-promotion.
        See Interstellar-code/hermes-agent#28.
        """
        name = name.strip()
        if not name:
            return
        with self._lock:
            self._promoted_servers.add(name)

    def is_server_promoted(self, name: str) -> bool:
        with self._lock:
            return name.strip() in self._promoted_servers

    def promoted_servers_snapshot(self) -> frozenset:
        """Immutable view of currently-promoted server names."""
        with self._lock:
            return frozenset(self._promoted_servers)

    def clear_servers(self) -> None:
        """Drop all promoted servers — called on mode flip or session reset."""
        with self._lock:
            self._promoted_servers.clear()

    def clear(self) -> None:
        """Drop all promoted tools, servers, and prev_mode — used by explicit session reset."""
        with self._lock:
            self._promoted.clear()
            self._promoted_servers.clear()
            self._prev_mode = None
            self._turn = 0
            self._last_used.clear()
            self._lazy_active = True


# Module-level registry. WeakValueDictionary so dropped sessions GC
# naturally; explicit ``evict()`` for deterministic cleanup at /new.
_pools: "weakref.WeakValueDictionary[str, DeferredToolPool]" = weakref.WeakValueDictionary()
# We must keep strong refs *somewhere* otherwise the weak dict drops
# every entry the moment we hand it back. The agent itself holds the
# pool via its ``mcp_lazy_pool`` attribute (see hook_impl.attach_pool).
# This list exists only so the weak dict doesn't lose entries while a
# pool is being created and before the agent attaches.
_strong_recent: list = []
_strong_recent_lock = threading.Lock()
_STRONG_RECENT_MAX = 32


def get_pool(session_id: str) -> DeferredToolPool:
    """Return the pool for ``session_id``, creating one if absent.

    Called from the lazy-loading hook on every relevant request, so
    must be cheap.

    Uses double-checked locking to avoid a check-then-set race where two
    concurrent calls both see ``_pools.get()`` return None and both create
    a new pool — with the second silently clobbering the first's promotions.
    The outer check is a fast unlocked read for the common already-exists
    case; the inner check under the lock handles the race.
    See Interstellar-code/hermes-agent#30.
    """
    if not session_id:
        # Unknown / unset session — use a single shared "unattributed"
        # pool. Better than crashing; promotion still works, only
        # isolation degrades.
        session_id = "__unattributed__"
    pool = _pools.get(session_id)
    if pool is not None:
        return pool
    with _strong_recent_lock:
        # Re-check under the lock — another thread may have created it
        # between our unlocked read and acquiring the lock.
        pool = _pools.get(session_id)
        if pool is None:
            pool = DeferredToolPool(session_id)
            _pools[session_id] = pool
            _strong_recent.append(pool)
            if len(_strong_recent) > _STRONG_RECENT_MAX:
                _strong_recent.pop(0)
    return pool


def evict(session_id: str) -> None:
    """Drop the pool for ``session_id`` immediately.

    Called from the ``pre_session_reset`` hook at ``cli.py:5900``
    before the old session's ``end_session()`` fires.
    """
    pool = _pools.pop(session_id, None)
    if pool is not None:
        pool.clear()  # clears both _promoted and _promoted_servers
        with _strong_recent_lock:
            try:
                _strong_recent.remove(pool)
            except ValueError:
                pass
        logger.debug("mcp_lazy: evicted pool for session %s", session_id)


def _reset_for_tests() -> None:
    """Test-only hard reset of the registry."""
    _pools.clear()
    with _strong_recent_lock:
        _strong_recent.clear()
