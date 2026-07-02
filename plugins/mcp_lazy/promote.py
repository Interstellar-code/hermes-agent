"""Promotion — switch an MCP tool or server from stub to full schema.

Promotion does NOT rebuild ``agent.tools``: that list stays the
canonical full set. Instead the per-session pool records which names
are promoted, and the request-time hook (``hook_impl.transform_tools``)
substitutes stubs for everything in ``agent.tools`` *except* what the
pool says is promoted.

This keeps the model very simple — one place to read the answer
("what tools are full for this session?") and one place to mutate it
("add this name to the promoted set"). Compared to a rebuild-based
design, we avoid all the cache-key / valid_tool_names / threading
edge cases.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List

from .pool import get_pool
from .stubs import MCP_PREFIX, _server_in_set

logger = logging.getLogger(__name__)

_DEFAULT_EAGER_TOKEN_THRESHOLD = 1500


def _load_eager_threshold() -> int:
    """Read ``mcp.server_eager_token_threshold`` (default 1500)."""
    try:
        from hermes_cli.config import load_config  # noqa: PLC0415
        mcp_cfg = load_config().get("mcp", {}) or {}
        val = mcp_cfg.get("server_eager_token_threshold", _DEFAULT_EAGER_TOKEN_THRESHOLD)
        return int(val) if val is not None else _DEFAULT_EAGER_TOKEN_THRESHOLD
    except Exception:
        return _DEFAULT_EAGER_TOKEN_THRESHOLD


def _estimate_server_full_tokens(agent: object, server_name: str) -> int:
    """Sum approximate token cost of full schemas for one server's tools.

    Uses ``len(json.dumps(schema)) // 4`` as a cheap, tokenizer-free
    approximation (matches the heuristic the cache_report script uses).
    Returns 0 when ``agent.tools`` is unavailable so the threshold check
    fails closed (treats unknown cost as 'too small to bother stubbing').
    """
    tools = getattr(agent, "tools", None)
    if not isinstance(tools, list):
        return 0
    total = 0
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = t.get("function", {}).get("name", "")
        if not isinstance(name, str) or not name.startswith(MCP_PREFIX):
            continue
        if not _server_in_set(name, {server_name}):
            continue
        try:
            total += len(json.dumps(t)) // 4
        except Exception:
            continue
    return total


def promote_tools(agent, tool_names: Iterable[str]) -> List[str]:
    """Promote ``tool_names`` in the session's pool.

    Returns the de-duplicated list of names actually promoted. Names
    that are not registered MCP tools (i.e. not in
    ``agent.valid_tool_names``) are silently dropped so a hallucinated
    request doesn't crash the call — the caller checks the return
    value if it cares.
    """
    session_id = getattr(agent, "session_id", None) or "__unattributed__"
    valid = getattr(agent, "valid_tool_names", None) or set()
    pool = get_pool(session_id)

    accepted: List[str] = []
    for name in tool_names:
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name:
            continue
        if valid and name not in valid:
            logger.debug(
                "mcp_lazy: refusing to promote unknown tool '%s' (not in valid_tool_names)",
                name,
            )
            continue
        accepted.append(name)
    if accepted:
        pool.promote(accepted)
        logger.debug(
            "mcp_lazy: promoted %d tool(s) for session %s",
            len(accepted), session_id,
        )
    return accepted


def promote_server_tools(agent: object, server_names: Iterable[str], *, eager: bool = False) -> List[str]:
    """Promote one or more MCP servers in the session's pool.

    Records the server name(s) in the pool's ``promoted_servers`` set so
    that the next ``transform_tools`` call will emit tool stubs for those
    servers (or full schemas when ``eager=True`` and the server is small
    enough per the token threshold).

    Returns the de-duplicated list of server names actually promoted.
    Unknown / empty names are silently dropped.

    Server → tool expansion uses ``agent.valid_tool_names`` (NOT a
    phantom ``agent.get_tool_definitions()``).  We look for MCP tool
    names whose ``mcp_{server}_`` prefix matches the requested server.
    """
    session_id = getattr(agent, "session_id", None) or "__unattributed__"
    valid = getattr(agent, "valid_tool_names", None) or set()
    pool = get_pool(session_id)

    accepted: List[str] = []
    for name in server_names:
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name:
            continue

        # Verify at least one real MCP tool exists for this server.
        # This prevents model hallucinations from polluting the pool.
        if valid:
            has_tool = any(
                t.startswith(MCP_PREFIX) and _server_in_set(t, {name})
                for t in valid
            )
            if not has_tool:
                logger.debug(
                    "mcp_lazy: refusing to promote unknown server '%s' (no matching tools in valid_tool_names)",
                    name,
                )
                continue

        # Q1: gate eager promotion on token cost.  If the model asked for
        # eager but the server's full-schema cost exceeds the configured
        # threshold, degrade silently to tool-stub mode (eager=False) so
        # one server doesn't blow the savings budget for the whole turn.
        effective_eager = eager
        if eager:
            threshold = _load_eager_threshold()
            cost = _estimate_server_full_tokens(agent, name)
            if cost > threshold:
                logger.info(
                    "mcp_lazy: server '%s' eager promote denied — full cost %d tok "
                    "exceeds server_eager_token_threshold=%d; falling back to tool stubs",
                    name, cost, threshold,
                )
                effective_eager = False

        pool.promote_server(name, eager=effective_eager)

        # When eager survives the threshold check, also promote each of
        # the server's tools to full schema (add to _promoted set).  This
        # is what makes ``eager=True`` actually different from the default
        # tool-stub path at render time.
        if effective_eager and valid:
            tool_names = [
                t for t in valid
                if isinstance(t, str)
                and t.startswith(MCP_PREFIX)
                and _server_in_set(t, {name})
            ]
            if tool_names:
                pool.promote(tool_names)
                logger.debug(
                    "mcp_lazy: eager-promoted %d tool(s) for server '%s'",
                    len(tool_names), name,
                )

        accepted.append(name)
        logger.debug(
            "mcp_lazy: promoted server '%s' (requested_eager=%r effective_eager=%r) for session %s",
            name, eager, effective_eager, session_id,
        )

    return accepted
