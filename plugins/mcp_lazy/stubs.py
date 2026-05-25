"""Stub-schema construction + detection.

A *stub* is a minimal OpenAI-format tool schema sent to the model in
place of the full MCP tool schema. It carries the name and a
truncated description but no real parameter spec — just a sentinel
field so we can identify stub-calls when the model invokes them.

Why a sentinel field rather than args-based detection: Hermes already
canonicalizes empty/whitespace tool args to ``"{}"`` at
``agent/conversation_loop.py:3103-3105``, which would make
"empty args = stub call" indistinguishable from legitimate zero-arg
MCP tools like ``mcp_zai_web_search_search``. The sentinel lives in
the schema, not the call, so detection happens on the schema we
registered — not on what the model sent back.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set

LAZY_SENTINEL = "__lazy_stub__"
LAZY_DESC_PREFIX = "[LAZY] "

# Tool names beginning with this prefix are MCP tools by convention.
# Universal across the codebase (see ``tools/mcp_tool.py:3316``
# ``is_mcp_tool_parallel_safe`` which uses the same check).
MCP_PREFIX = "mcp_"


def is_mcp_tool(schema: Dict[str, Any]) -> bool:
    """Return True if the schema is for an MCP-sourced tool."""
    name = schema.get("function", {}).get("name", "")
    return isinstance(name, str) and name.startswith(MCP_PREFIX)


def is_stub_schema(schema: Dict[str, Any]) -> bool:
    """Return True if ``schema`` is a stub we produced.

    Checked against the schema we built — not against the model's
    tool-call args. Real zero-arg MCP tools have no sentinel and so
    are never flagged as stubs.
    """
    params = schema.get("function", {}).get("parameters", {})
    if not isinstance(params, dict):
        return False
    props = params.get("properties", {})
    return isinstance(props, dict) and LAZY_SENTINEL in props


def make_stub_schema(full: Dict[str, Any], max_desc: int = 200) -> Dict[str, Any]:
    """Build a stub schema from a full MCP tool schema.

    Preserves the name; truncates the description; replaces parameters
    with a single sentinel field so we can detect stub-calls later.

    ``max_desc`` is intentionally small — stub size is the whole point.
    Default 200 chars matches ``mcp.lazy_stub_max_desc`` config default.
    """
    func = full.get("function", {})
    name = func.get("name", "")
    desc = (func.get("description", "") or "")[:max_desc]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{LAZY_DESC_PREFIX}{desc}" if desc else LAZY_DESC_PREFIX.strip(),
            "parameters": {
                "type": "object",
                "properties": {
                    LAZY_SENTINEL: {
                        "type": "boolean",
                        "const": True,
                        "description": (
                            "Internal marker — do not set. Calling this stub "
                            "will trigger full-schema load + auto-retry."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
    }


def mix_full_and_stubs(
    all_tools: List[Dict[str, Any]],
    *,
    promoted_names: "Iterable[str] | Set[str] | frozenset",
    lazy_servers: "Set[str] | None" = None,
    max_desc: int = 200,
) -> List[Dict[str, Any]]:
    """Return a new tool list: builtins full, MCP either stubbed or full.

    - Builtin tools (no ``mcp_`` prefix) pass through unchanged.
    - MCP tools whose name is in ``promoted_names`` pass through full.
    - All other MCP tools are replaced with stubs.
    - ``lazy_servers`` (optional): when set, only stub MCP tools whose
      server name is in the set. MCP tool names have the shape
      ``mcp_{server}_{tool}``; we extract the server segment for the
      per-server toggle.
    """
    promoted = set(promoted_names) if not isinstance(promoted_names, (set, frozenset)) else promoted_names
    result: List[Dict[str, Any]] = []
    for tool in all_tools:
        if not is_mcp_tool(tool):
            result.append(tool)
            continue
        name = tool.get("function", {}).get("name", "")
        if name in promoted:
            result.append(tool)
            continue
        if lazy_servers is not None and not _server_in_set(name, lazy_servers):
            # Per-server toggle says this server stays eager.
            result.append(tool)
            continue
        result.append(make_stub_schema(tool, max_desc=max_desc))
    return result


def _server_in_set(tool_name: str, server_set: "Set[str] | frozenset") -> bool:
    """Extract the server segment of an MCP tool name and check membership.

    ``mcp_{server}_{rest}``; server may itself contain underscores after
    sanitization. We match by prefix: any registered server in the set
    whose canonical form is a prefix of the trailing tool name segment.
    """
    if not tool_name.startswith(MCP_PREFIX):
        return False
    rest = tool_name[len(MCP_PREFIX):]
    for server in server_set:
        sanitised = server.replace("-", "_").replace(".", "_")
        if rest == sanitised or rest.startswith(sanitised + "_"):
            return True
    return False
