"""Tests for server_stubs — stub schema construction, description synthesis,
and discovery-mode branching in mix_full_and_stubs."""
from __future__ import annotations

import pytest

from plugins.mcp_lazy.server_stubs import (
    SERVER_LAZY_SENTINEL,
    SERVER_STUB_NAME_PREFIX,
    derive_servers_from_tools,
    is_server_stub_schema,
    make_server_stub_schema,
    synth_server_description,
)
from plugins.mcp_lazy.stubs import is_stub_schema, mix_full_and_stubs


def _full(name: str, description: str = "A real tool"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
        },
    }


# ── make_server_stub_schema ──────────────────────────────────────────────────


def test_server_stub_name_prefix():
    stub = make_server_stub_schema("trek", "Trip tools", tool_count=5)
    assert stub["function"]["name"] == f"{SERVER_STUB_NAME_PREFIX}trek"


def test_server_stub_sanitises_name():
    stub = make_server_stub_schema("my-server.v2", "Desc", tool_count=1)
    assert stub["function"]["name"] == f"{SERVER_STUB_NAME_PREFIX}my_server_v2"


def test_server_stub_has_sentinel():
    stub = make_server_stub_schema("trek", "Desc", tool_count=3)
    props = stub["function"]["parameters"]["properties"]
    assert SERVER_LAZY_SENTINEL in props


def test_server_stub_truncates_description():
    long_desc = "x" * 300
    stub = make_server_stub_schema("srv", long_desc, tool_count=1, max_desc=50)
    assert len(stub["function"]["description"]) <= 50


def test_server_stub_metadata_fields():
    stub = make_server_stub_schema("trek", "Trip tools", tool_count=7)
    assert stub["_server_name"] == "trek"
    assert stub["_tool_count"] == 7


# ── is_server_stub_schema ────────────────────────────────────────────────────


def test_is_server_stub_detects_sentinel():
    stub = make_server_stub_schema("trek", "Desc", tool_count=2)
    assert is_server_stub_schema(stub)


def test_is_server_stub_rejects_tool_stub():
    from plugins.mcp_lazy.stubs import make_stub_schema
    tool_stub = make_stub_schema(_full("mcp_trek_search"))
    assert not is_server_stub_schema(tool_stub)


def test_is_server_stub_rejects_full_tool():
    assert not is_server_stub_schema(_full("mcp_trek_search"))


# ── synth_server_description ─────────────────────────────────────────────────


def test_synth_description_format():
    names = ["mcp_trek_search_files", "mcp_trek_create_trip", "mcp_trek_delete_trip", "mcp_trek_get_info"]
    desc = synth_server_description(names)
    assert desc.startswith("4 tools:")
    assert "search files" in desc
    assert "…" in desc


def test_synth_description_three_or_fewer_no_ellipsis():
    names = ["mcp_gmail_send", "mcp_gmail_draft"]
    desc = synth_server_description(names)
    assert "2 tools:" in desc
    assert "…" not in desc


def test_synth_description_zero_tools():
    desc = synth_server_description([])
    assert desc == "0 tools"


def test_synth_description_max_chars():
    names = ["mcp_srv_" + ("x" * 40) + "_action"] * 10
    desc = synth_server_description(names, max_chars=50)
    assert len(desc) <= 50


# ── derive_servers_from_tools ────────────────────────────────────────────────


def test_derive_servers_groups_by_prefix():
    tools = [_full("mcp_trek_search"), _full("mcp_trek_create"), _full("mcp_gmail_send")]
    result = derive_servers_from_tools(tools)
    assert "trek" in result
    assert "gmail" in result
    assert len(result["trek"]) == 2
    assert len(result["gmail"]) == 1


def test_derive_servers_ignores_non_mcp():
    tools = [_full("terminal"), _full("read_file"), _full("mcp_trek_x")]
    result = derive_servers_from_tools(tools)
    assert list(result.keys()) == ["trek"]


# ── mix_full_and_stubs with discovery_mode ───────────────────────────────────


def test_mix_server_mode_emits_one_stub_per_server():
    tools = [
        _full("terminal"),
        _full("mcp_trek_search"),
        _full("mcp_trek_create"),
        _full("mcp_gmail_send"),
    ]
    result = mix_full_and_stubs(
        tools,
        promoted_names=frozenset(),
        lazy_servers={"trek", "gmail"},
        discovery_mode="server",
    )
    # terminal passes through; two server stubs; no individual tool entries
    names = [t["function"]["name"] for t in result]
    assert "terminal" in names
    assert f"{SERVER_STUB_NAME_PREFIX}trek" in names
    assert f"{SERVER_STUB_NAME_PREFIX}gmail" in names
    assert "mcp_trek_search" not in names
    assert "mcp_trek_create" not in names
    assert "mcp_gmail_send" not in names


def test_mix_both_mode_unpromoted_server_shows_only_server_stub():
    tools = [_full("mcp_trek_search"), _full("mcp_trek_create")]
    result = mix_full_and_stubs(
        tools,
        promoted_names=frozenset(),
        lazy_servers={"trek"},
        discovery_mode="both",
        promoted_servers=frozenset(),
    )
    names = [t["function"]["name"] for t in result]
    assert f"{SERVER_STUB_NAME_PREFIX}trek" in names
    assert "mcp_trek_search" not in names


def test_mix_both_mode_promoted_server_hides_stub_and_shows_tool_stubs():
    """After server promotion the discovery stub must be retired.

    See Interstellar-code/hermes-agent#18 — leaving ``mcp_server_<name>``
    visible alongside the concrete tool stubs let the model keep routing
    to the discovery stub in a loop. Promotion swaps the surface for
    per-tool stubs (or full schemas) instead.
    """
    tools = [_full("mcp_trek_search"), _full("mcp_trek_create")]
    result = mix_full_and_stubs(
        tools,
        promoted_names=frozenset(),
        lazy_servers={"trek"},
        discovery_mode="both",
        promoted_servers=frozenset({"trek"}),
    )
    names = [t["function"]["name"] for t in result]
    assert f"{SERVER_STUB_NAME_PREFIX}trek" not in names
    assert "mcp_trek_search" in names
    assert "mcp_trek_create" in names
    # Tool entries should be stubs (not full), since they're not in promoted_names
    tool_entries = [t for t in result if t["function"]["name"] == "mcp_trek_search"]
    assert is_stub_schema(tool_entries[0])


def test_mix_both_mode_individually_promoted_tool_is_full():
    tools = [_full("mcp_trek_search"), _full("mcp_trek_create")]
    result = mix_full_and_stubs(
        tools,
        promoted_names=frozenset({"mcp_trek_search"}),
        lazy_servers={"trek"},
        discovery_mode="both",
        promoted_servers=frozenset({"trek"}),
    )
    search_entries = [t for t in result if t["function"]["name"] == "mcp_trek_search"]
    assert search_entries
    assert not is_stub_schema(search_entries[0])


def test_mix_tool_mode_unchanged_behaviour():
    """tool mode must produce identical output to Phase 1."""
    tools = [_full("terminal"), _full("mcp_trek_search")]
    out_phase1 = mix_full_and_stubs(tools, promoted_names=frozenset(), lazy_servers={"trek"})
    out_phase2 = mix_full_and_stubs(
        tools, promoted_names=frozenset(), lazy_servers={"trek"}, discovery_mode="tool"
    )
    assert out_phase1 == out_phase2


def test_mix_server_mode_uses_config_description():
    tools = [_full("mcp_trek_search")]
    result = mix_full_and_stubs(
        tools,
        promoted_names=frozenset(),
        lazy_servers={"trek"},
        discovery_mode="server",
        server_descriptions={"trek": "Custom trip planning description"},
    )
    server_stubs = [t for t in result if is_server_stub_schema(t)]
    assert server_stubs
    assert "Custom trip planning description" in server_stubs[0]["function"]["description"]
