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

from typing import Any, Dict, Iterable, List, Optional, Set

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
    discovery_mode: str = "tool",
    promoted_servers: "Optional[frozenset]" = None,
    server_descriptions: "Optional[Dict[str, str]]" = None,
    server_stub_max_desc: int = 150,
) -> List[Dict[str, Any]]:
    """Return a new tool list: builtins full, MCP either stubbed or full.

    - Builtin tools (no ``mcp_`` prefix) pass through unchanged.
    - MCP tools whose name is in ``promoted_names`` pass through full.
    - All other MCP tools are replaced with stubs.
    - ``lazy_servers`` (optional): when set, only stub MCP tools whose
      server name is in the set. MCP tool names have the shape
      ``mcp_{server}_{tool}``; we extract the server segment for the
      per-server toggle.

    When ``discovery_mode`` is ``"server"`` or ``"both"``, one stub per
    MCP server is emitted instead of (or alongside) per-tool stubs.

    ``discovery_mode`` values:
    - ``"tool"`` (default): Phase 1 behaviour — per-tool stubs.
    - ``"server"``: one server stub per eligible server; individual tool
      entries omitted entirely.
    - ``"both"``: server stubs always present; tool stubs emitted for
      servers in ``promoted_servers``; unpromoted servers omit tools.
    """
    from .server_stubs import (  # noqa: PLC0415
        derive_servers_from_tools,
        is_server_stub_schema,
        make_server_stub_schema,
        synth_server_description,
    )

    promoted = set(promoted_names) if not isinstance(promoted_names, (set, frozenset)) else promoted_names
    p_servers: frozenset = promoted_servers if promoted_servers is not None else frozenset()
    descs: Dict[str, str] = server_descriptions or {}

    if discovery_mode == "tool":
        # Phase 1 path — unchanged behaviour.
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
                result.append(tool)
                continue
            result.append(make_stub_schema(tool, max_desc=max_desc))
        return result

    # "server" or "both" mode — group MCP tools by server.
    mcp_tools = [t for t in all_tools if is_mcp_tool(t)]
    non_mcp = [t for t in all_tools if not is_mcp_tool(t)]

    # Build server → [tools] mapping from the actual tool list.
    server_tool_map: Dict[str, List[Dict[str, Any]]] = {}
    for tool in mcp_tools:
        name = tool.get("function", {}).get("name", "")
        server = _extract_server(name, lazy_servers)
        if server is None:
            # Tool's server not in eligible set → pass through full.
            continue
        server_tool_map.setdefault(server, []).append(tool)

    # Tools whose server wasn't found in eligible set pass through unchanged.
    ineligible = [
        t for t in mcp_tools
        if _extract_server(t.get("function", {}).get("name", ""), lazy_servers) is None
    ]

    result = list(non_mcp) + list(ineligible)

    # Emit server stubs for each eligible server.
    for server, srv_tools in sorted(server_tool_map.items()):
        # Once a server is promoted, retire its discovery stub so the model
        # cannot select ``mcp_server_<name>`` alongside the promoted tools.
        # Keeping both visible reinforces a routing loop where the stub keeps
        # winning even though the concrete tools are already available. See
        # Interstellar-code/hermes-agent#18.
        #
        # The discovery stub must ALSO retire when individual tools of this
        # server have been promoted without the server itself being promoted
        # (e.g. ``pre_tool_call`` auto-promotes a single tool via
        # ``promote_tools``, which never touches ``_promoted_servers``; a
        # subsequent discovery_mode flip to "both" would otherwise re-surface
        # the stub AND drop the already-promoted tool). Keying retirement on
        # per-tool promotions as well closes that gap. See follow-up to #18.
        has_promoted_tool = any(
            t.get("function", {}).get("name", "") in promoted for t in srv_tools
        )
        if discovery_mode == "both" and (server in p_servers or has_promoted_tool):
            # Server (or at least one of its tools) promoted → emit tool stubs
            # (or full if promoted) and skip the discovery stub entirely.
            # Promoted tools provide the surface.
            for tool in srv_tools:
                name = tool.get("function", {}).get("name", "")
                if name in promoted:
                    result.append(tool)
                else:
                    result.append(make_stub_schema(tool, max_desc=max_desc))
            continue

        tool_names = [t.get("function", {}).get("name", "") for t in srv_tools]
        desc = descs.get(server) or synth_server_description(tool_names, max_chars=server_stub_max_desc)
        stub = make_server_stub_schema(
            server_name=server,
            description=desc,
            tool_count=len(srv_tools),
            max_desc=server_stub_max_desc,
        )
        result.append(stub)
        # In ``"server"`` mode AND unpromoted ``"both"``: tool entries omitted.

    return result


def _extract_server(tool_name: str, lazy_servers: "Optional[Set[str]]") -> "Optional[str]":
    """Return the matched server name for a tool, or None if ineligible.

    When ``lazy_servers`` is None, every MCP tool is eligible and we
    use the first segment after ``mcp_`` as the server name.
    """
    if not tool_name.startswith(MCP_PREFIX):
        return None
    rest = tool_name[len(MCP_PREFIX):]
    if lazy_servers is None:
        # All MCP tools eligible; server = first underscore-segment.
        return rest.split("_", 1)[0] if "_" in rest else rest
    # Match against eligible servers (longest match wins — EDGE #2 fix).
    candidates = sorted(lazy_servers, key=lambda s: len(s.replace("-", "_").replace(".", "_")), reverse=True)
    for server in candidates:
        sanitised = server.replace("-", "_").replace(".", "_")
        if rest == sanitised or rest.startswith(sanitised + "_"):
            return server
    return None


def _server_in_set(tool_name: str, server_set: "Set[str] | frozenset") -> bool:
    """Extract the server segment of an MCP tool name and check membership.

    ``mcp_{server}_{rest}``; server may itself contain underscores after
    sanitization. We match by prefix: any registered server in the set
    whose canonical form is a prefix of the trailing tool name segment.

    Servers are checked in descending sanitised-name-length order so that
    ``my_tool_v2`` matches before ``my_tool`` on a tool like
    ``mcp_my_tool_v2_create`` (EDGE #2 collision fix).
    """
    if not tool_name.startswith(MCP_PREFIX):
        return False
    rest = tool_name[len(MCP_PREFIX):]
    candidates = sorted(server_set, key=lambda s: len(s.replace("-", "_").replace(".", "_")), reverse=True)
    for server in candidates:
        sanitised = server.replace("-", "_").replace(".", "_")
        if rest == sanitised or rest.startswith(sanitised + "_"):
            return True
    return False
