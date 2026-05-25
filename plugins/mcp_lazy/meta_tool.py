"""``mcp_load_tools`` meta-tool.

Registered only when ``mcp.lazy_loading: true``. Lets the model
request the full parameter schemas for one or more MCP tools by name.
Once promoted, the tools stay full for the rest of the session.

Usage from the model::

    mcp_load_tools(tool_names=["mcp_trek_search_files", "mcp_dart_get_task"])

The call returns a JSON status string. The model is expected to wait
for the next turn (where the full schemas will be present in the
tool list) before invoking the promoted tools with real parameters.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from .promote import promote_tools

SCHEMA: Dict[str, Any] = {
    # Name deliberately does NOT start with ``mcp_`` so the stub
    # filter in ``stubs.is_mcp_tool`` never collapses this tool to
    # a stub. If it were ``mcp_load_tools`` it would target itself.
    "name": "load_mcp_tools",
    "description": (
        "Load full parameter schemas for MCP tools by name. Use when "
        "you need to call an MCP tool whose visible schema is a [LAZY] "
        "stub. After this call returns, the next turn will see the "
        "full parameter spec for each promoted tool; invoke the tool "
        "normally on that turn."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tool_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of MCP tool names to load full schemas for "
                    "(e.g. ['mcp_trek_search_files'])."
                ),
            },
        },
        "required": ["tool_names"],
    },
}


def check() -> bool:
    """Always-on once the plugin is enabled; auth happens at request time."""
    return True


async def handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """Promote the named tools in the current session's pool.

    Agent is resolved (in order) from ``kwargs["_agent"]``,
    ``kwargs["agent"]``, then the ``mcp_lazy`` plugin's per-request
    ContextVar set by ``hook_impl.transform_tools``. The fallback is
    necessary because ``registry.dispatch`` does not natively forward
    the agent reference to tool handlers.
    """
    agent = kwargs.get("_agent") or kwargs.get("agent")
    if agent is None:
        try:
            from .pool import _current_agent_var  # noqa: PLC0415
            agent = _current_agent_var.get()
        except Exception:
            agent = None

    raw_names = args.get("tool_names") or []
    if not isinstance(raw_names, list):
        return json.dumps({"ok": False, "error": "tool_names must be an array"})

    if agent is None:
        return json.dumps({
            "ok": False,
            "error": "mcp_load_tools: agent context unavailable",
        })

    accepted: List[str] = promote_tools(agent, raw_names)
    rejected = [n for n in raw_names if isinstance(n, str) and n.strip() and n.strip() not in accepted]

    return json.dumps({
        "ok": True,
        "promoted": accepted,
        "rejected": rejected,
        "note": (
            "Full schemas will be visible on the next turn. Call the "
            "promoted tools with proper parameters then."
        ),
    })
