"""Hook implementations for the mcp_lazy plugin.

* ``transform_tools`` — rewrites the agent's tool list at request time,
  stubbing MCP tools that haven't been promoted in this session.
* ``on_session_reset`` — drops the per-session pool when the user
  starts a new session (via ``/new`` or its ``/reset`` alias).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from .pool import _current_agent_var, evict, get_pool
from .stubs import mix_full_and_stubs

logger = logging.getLogger(__name__)


def _load_config() -> Dict[str, Any]:
    """Read ``mcp`` config block; tolerate missing config."""
    try:
        from hermes_cli.config import load_config  # noqa: PLC0415
        return load_config().get("mcp", {}) or {}
    except Exception:
        return {}


def _eligible_servers() -> Optional[Set[str]]:
    """Return the set of MCP server names that should be stubbed.

    Reads the top-level ``mcp_servers`` config block (the real shape
    of the Hermes config — there is no ``mcp.servers`` sub-block).
    Returns ``None`` when the config has no ``mcp_servers`` at all,
    which the caller interprets as "stub every MCP tool regardless
    of server" (the safest default for the master-toggle case).
    """
    try:
        from hermes_cli.config import load_config  # noqa: PLC0415
        mcp_servers = load_config().get("mcp_servers", {}) or {}
    except Exception:
        return None
    if not mcp_servers:
        return None
    eligible: Set[str] = set()
    for name, spec in mcp_servers.items():
        if isinstance(spec, dict) and spec.get("lazy") is False:
            # Per-server explicit opt-out.
            continue
        eligible.add(name)
    return eligible


def transform_tools(
    tools: List[Dict[str, Any]],
    agent: Any = None,
    **_kwargs: Any,
) -> Optional[List[Dict[str, Any]]]:
    """Return a tool list with un-promoted MCP tools replaced by stubs.

    Returns ``None`` when lazy loading is disabled, when the agent has
    no session_id, or on any internal error — letting the caller use
    the original ``tools`` list unchanged (fail-open).
    """
    try:
        cfg = _load_config()
        if not cfg.get("lazy_loading"):
            return None  # master toggle off

        session_id = getattr(agent, "session_id", None) if agent is not None else None
        if not session_id:
            return None

        pool = get_pool(session_id)
        # Anchor the pool on the agent so the registry's weak refs
        # don't lose it between calls.
        if agent is not None:
            try:
                setattr(agent, "_mcp_lazy_pool", pool)
            except Exception:
                pass

        # Stash the agent in a ContextVar so the load_mcp_tools
        # meta-tool handler can resolve it without registry.dispatch
        # having to plumb the agent reference explicitly. Tool
        # dispatch runs in the same task; contextvars propagate.
        try:
            _current_agent_var.set(agent)
        except Exception:
            pass

        return mix_full_and_stubs(
            tools,
            promoted_names=pool.snapshot(),
            lazy_servers=_eligible_servers(),
            max_desc=int(cfg.get("lazy_stub_max_desc", 200) or 200),
        )
    except Exception:
        logger.debug("mcp_lazy: transform_tools failed; returning None", exc_info=True)
        return None


def on_session_reset(session_id: str = None, **_kwargs: Any) -> None:
    """Drop the previous session's pool.

    cli.py fires this AFTER agent.session_id has rotated to the new
    id — at this point any pool keyed on the old id is dead weight.
    We can't know the old id from kwargs, but the registry's
    WeakValueDictionary will GC it automatically once the agent's
    `_mcp_lazy_pool` attribute is overwritten on next ``transform_tools``
    call.

    This handler exists primarily as an explicit hook for future
    extensions (e.g. emitting a "session reset" event to a dashboard);
    the cleanup itself is GC-driven.
    """
    # Best-effort: if the caller passed an old_session_id (extension),
    # evict immediately.
    old_id = _kwargs.get("old_session_id")
    if isinstance(old_id, str) and old_id:
        evict(old_id)
