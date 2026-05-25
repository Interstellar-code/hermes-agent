"""Promotion — switch an MCP tool from stub to full schema for the session.

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

import logging
from typing import Iterable, List

from .pool import get_pool

logger = logging.getLogger(__name__)


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
