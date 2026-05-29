"""Server-level stub schema construction + detection.

A *server stub* is a pseudo-tool that represents an entire MCP server.
It carries the server name, an auto-synthesised (or config-provided)
description, and a sentinel field distinct from the per-tool stub
sentinel so dispatch routing can differentiate the two.

The stub name is ``mcp_server_{sanitised_name}`` — deliberately NOT
in the ``mcp_`` tool namespace so the Phase 1 tool-stub filter never
collapses a server stub to a per-tool stub.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

SERVER_LAZY_SENTINEL = "__lazy_server_stub__"
SERVER_STUB_NAME_PREFIX = "mcp_server_"

# Internal metadata key written into every server-stub schema dict so that
# detection can use a sentinel rather than the ``mcp_server_`` name prefix.
# A real MCP server could legitimately be named "server", which would make its
# tools start with ``mcp_server_`` and its stub also named ``mcp_server_server``
# — identical to our synthetic stub for a server named "server".  The sentinel
# is the authoritative discriminator; the prefix is only cosmetic.
_IS_SERVER_STUB_KEY = "__is_server_stub__"


def _sanitise(name: str) -> str:
    """Sanitise a server name for use in a pseudo-tool name."""
    return re.sub(r"[^a-z0-9_]", "_", name.lower())


def make_server_stub_schema(
    server_name: str,
    description: str,
    tool_count: int,
    max_desc: int = 150,
) -> Dict[str, Any]:
    """Build a server-level stub schema.

    The pseudo-tool name is ``mcp_server_{sanitised_name}``.  The
    description is truncated to ``max_desc`` characters.  A sentinel
    parameter distinguishes this from per-tool stubs.
    """
    sanitised = _sanitise(server_name)
    desc = (description or "")[:max_desc]
    return {
        "type": "function",
        "function": {
            "name": f"{SERVER_STUB_NAME_PREFIX}{sanitised}",
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": {
                    SERVER_LAZY_SENTINEL: {
                        "type": "boolean",
                        "const": True,
                        "description": (
                            "Internal marker — do not set. "
                            "Call load_mcp_server to expand this server's tools."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
        # Metadata for internal use (not sent to model — removed at emit time
        # if the caller strips non-standard keys; kept for routing).
        "_server_name": server_name,
        "_tool_count": tool_count,
        # Sentinel key: authoritative marker that this dict is a server stub.
        # Use ``is_server_stub_schema()`` rather than checking the name prefix
        # because a real MCP server named "server" produces tool names that also
        # start with ``mcp_server_`` — the prefix alone is ambiguous.  See #27.
        _IS_SERVER_STUB_KEY: True,
    }


def is_server_stub_schema(schema: Dict[str, Any]) -> bool:
    """Return True if ``schema`` is a server stub we produced.

    Uses the ``_IS_SERVER_STUB_KEY`` sentinel written at construction time
    rather than the ``mcp_server_`` name prefix.  A real MCP server named
    "server" has tools that also start with ``mcp_server_``, making the name
    prefix ambiguous.  The sentinel is the authoritative discriminator.
    See Interstellar-code/hermes-agent#27.
    """
    if schema.get(_IS_SERVER_STUB_KEY):
        return True
    # Fallback: schema built before this sentinel existed (e.g. tests that
    # hand-craft a minimal dict).  Check the parameters sentinel as before.
    params = schema.get("function", {}).get("parameters", {})
    if not isinstance(params, dict):
        return False
    props = params.get("properties", {})
    return isinstance(props, dict) and SERVER_LAZY_SENTINEL in props


def derive_servers_from_tools(tools: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Group MCP tool names by inferred server name.

    Falls back to prefix-extraction when no ``mcp_servers`` config is
    available.  Returns ``{server_name: [tool_name, ...]}``.
    """
    servers: Dict[str, List[str]] = {}
    for tool in tools:
        name = tool.get("function", {}).get("name", "")
        if not isinstance(name, str) or not name.startswith("mcp_"):
            continue
        rest = name[4:]  # strip leading "mcp_"
        # First underscore-delimited segment is the server name.
        parts = rest.split("_", 1)
        server = parts[0] if parts else rest
        servers.setdefault(server, []).append(name)
    return servers


def synth_server_description(tool_names: List[str], max_chars: int = 150) -> str:
    """Auto-synthesise a server description from a list of tool names.

    Format: ``"{N} tools: name1, name2, name3, …"``
    Only the final segment of each name (after the server prefix) is used
    to keep the description readable.
    """
    n = len(tool_names)
    if n == 0:
        return "0 tools"
    # Humanise: strip the mcp_{server}_ prefix, replace underscores with spaces.
    humanised = []
    for t in tool_names:
        parts = t.split("_", 2)  # mcp, server, rest
        label = parts[2] if len(parts) == 3 else t
        humanised.append(label.replace("_", " "))
    sample = humanised[:3]
    base = f"{n} tools: {', '.join(sample)}"
    if n > 3:
        base += ", …"
    return base[:max_chars]
