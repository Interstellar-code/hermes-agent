"""``load_mcp_server`` meta-tool.

Registered when ``mcp.discovery_mode`` is ``"server"`` or ``"both"``.
Lets the model request tool-stub expansion for one or more MCP servers
by name.  After promotion the next turn's tool list will include Phase 1
tool stubs (or full schemas for tiny servers) for each promoted server.

Usage from the model::

    load_mcp_server(server_names=["trek", "gmail"])

Supports an optional ``eager`` flag (default false).  When true the
caller opts into full-schema promotion for the server's tools rather
than per-tool stubs; useful when the model knows it will need many tools
from the server in the same turn.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from .promote import promote_server_tools

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    # Does NOT start with ``mcp_`` so the stub filter never collapses it.
    "name": "load_mcp_server",
    "description": (
        "Load tool stubs for one or more MCP servers by name.  Use when "
        "you see a server stub (mcp_server_<name>) and need to access its "
        "tools.  After this call the next turn will show tool stubs for "
        "each promoted server.  Pass eager=true to load full schemas "
        "immediately (use only when you need many tools from one server)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "server_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of MCP server names to expand "
                    "(e.g. ['trek', 'gmail'])."
                ),
            },
            "eager": {
                "type": "boolean",
                "description": (
                    "When true, promote to full schemas instead of tool stubs. "
                    "Default false.  Use only when you need many tools from the "
                    "server in the same session."
                ),
            },
        },
        "required": ["server_names"],
    },
}


def check() -> bool:
    """Always-on once the plugin is enabled."""
    return True


async def handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """Promote the named servers in the current session's pool.

    Agent is resolved from ``kwargs["_agent"]``, ``kwargs["agent"]``,
    then the ``mcp_lazy`` ContextVar set by ``hook_impl.transform_tools``.
    """
    agent = kwargs.get("_agent") or kwargs.get("agent")
    if agent is None:
        try:
            from .pool import _current_agent_var  # noqa: PLC0415
            agent = _current_agent_var.get()
        except Exception:
            agent = None

    raw_names = args.get("server_names") or []
    if not isinstance(raw_names, list):
        return json.dumps({"ok": False, "error": "server_names must be an array"})

    eager = bool(args.get("eager", False))

    if agent is None:
        return json.dumps({
            "ok": False,
            "error": "load_mcp_server: agent context unavailable",
        })

    promoted: List[str] = promote_server_tools(agent, raw_names, eager=eager)
    rejected = [
        n for n in raw_names
        if isinstance(n, str) and n.strip() and n.strip() not in promoted
    ]

    return json.dumps({
        "ok": True,
        "promoted": promoted,
        "rejected": rejected,
        "available_next_turn": True,
        "note": (
            "Tool stubs for the promoted servers will be visible on the "
            "next turn.  Call load_mcp_tools on individual tools when you "
            "need their full parameter schemas."
        ),
    })
