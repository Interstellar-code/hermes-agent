"""Edge cases: 0-tool server, nonexistent server, missing desc synth, prefix collision."""
from __future__ import annotations

import pytest

from plugins.mcp_lazy.server_stubs import (
    SERVER_STUB_NAME_PREFIX,
    is_server_stub_schema,
    make_server_stub_schema,
    synth_server_description,
)
from plugins.mcp_lazy.stubs import (
    _server_in_set,
    mix_full_and_stubs,
)
from plugins.mcp_lazy.pool import _reset_for_tests


def _full(name: str):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Desc",
            "parameters": {"type": "object", "properties": {}},
        },
    }


@pytest.fixture(autouse=True)
def reset():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_zero_tool_server_description():
    desc = synth_server_description([])
    assert desc == "0 tools"


def test_zero_tool_server_stub_schema():
    stub = make_server_stub_schema("empty_srv", "", tool_count=0)
    assert is_server_stub_schema(stub)
    assert stub["_tool_count"] == 0


def test_server_mode_no_tools_for_server():
    """Server with 0 matching tools: no server stub emitted."""
    tools = [_full("mcp_other_tool")]
    result = mix_full_and_stubs(
        tools,
        promoted_names=frozenset(),
        lazy_servers={"trek"},  # trek has no tools in list
        discovery_mode="server",
    )
    names = [t["function"]["name"] for t in result]
    assert f"{SERVER_STUB_NAME_PREFIX}trek" not in names


def test_nonexistent_server_not_promoted():
    """promote_server_tools with a server that has no valid tools is rejected."""
    from plugins.mcp_lazy.promote import promote_server_tools

    class _Agent:
        session_id = "edge-test"
        valid_tool_names = {"mcp_trek_search", "mcp_gmail_send"}

    accepted = promote_server_tools(_Agent(), ["nonexistent_xyz_server"])
    assert accepted == []


def test_missing_description_synth_fallback():
    """No config description → auto-synth used; format is correct."""
    tools = [_full("mcp_trek_search"), _full("mcp_trek_create")]
    result = mix_full_and_stubs(
        tools,
        promoted_names=frozenset(),
        lazy_servers={"trek"},
        discovery_mode="server",
        server_descriptions={},  # no config description
    )
    stubs = [t for t in result if is_server_stub_schema(t)]
    assert stubs
    assert "tools:" in stubs[0]["function"]["description"]


def test_prefix_collision_longer_wins():
    """EDGE #2: mcp_my_tool_v2_create must route to my_tool_v2, not my_tool."""
    assert _server_in_set("mcp_my_tool_v2_create", {"my_tool", "my_tool_v2"})
    # Verify it matches the longer one specifically via _extract_server
    from plugins.mcp_lazy.stubs import _extract_server
    matched = _extract_server("mcp_my_tool_v2_create", {"my_tool", "my_tool_v2"})
    assert matched == "my_tool_v2"


def test_prefix_collision_shorter_still_matches_own_tools():
    """mcp_my_tool_do must route to my_tool, not my_tool_v2."""
    from plugins.mcp_lazy.stubs import _extract_server
    matched = _extract_server("mcp_my_tool_do", {"my_tool", "my_tool_v2"})
    assert matched == "my_tool"


def test_server_stub_description_truncated_at_max():
    stub = make_server_stub_schema("s", "x" * 200, tool_count=1, max_desc=100)
    assert len(stub["function"]["description"]) <= 100


def test_synth_exactly_three_tools_no_ellipsis():
    names = ["mcp_srv_a", "mcp_srv_b", "mcp_srv_c"]
    desc = synth_server_description(names)
    assert "…" not in desc
    assert "3 tools:" in desc
