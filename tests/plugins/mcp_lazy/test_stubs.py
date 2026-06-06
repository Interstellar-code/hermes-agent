"""Tests for stub schema construction + detection + mix."""
from __future__ import annotations

import pytest

from plugins.mcp_lazy.stubs import (
    LAZY_SENTINEL,
    is_mcp_tool,
    is_stub_schema,
    make_stub_schema,
    mix_full_and_stubs,
)


def _full(name: str, description: str = "A real tool", params=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": params or {"type": "object", "properties": {"x": {"type": "string"}}},
        },
    }


def test_is_mcp_tool_by_prefix():
    assert is_mcp_tool(_full("mcp_trek_search"))
    assert not is_mcp_tool(_full("terminal"))
    assert not is_mcp_tool(_full("read_file"))


def test_make_stub_preserves_name():
    full = _full("mcp_x_y", "X-ray scanner")
    stub = make_stub_schema(full)
    assert stub["function"]["name"] == "mcp_x_y"


def test_make_stub_marks_description():
    stub = make_stub_schema(_full("mcp_a_b", "Original description"))
    assert stub["function"]["description"].startswith("[LAZY]")
    assert "Original description" in stub["function"]["description"]


def test_make_stub_truncates_description():
    long_desc = "x" * 500
    stub = make_stub_schema(_full("mcp_a_b", long_desc), max_desc=50)
    # Should be [LAZY] + first 50 chars
    assert len(stub["function"]["description"]) <= len("[LAZY] ") + 50


def test_make_stub_injects_sentinel():
    stub = make_stub_schema(_full("mcp_a_b"))
    props = stub["function"]["parameters"]["properties"]
    assert LAZY_SENTINEL in props


def test_is_stub_schema_detects_sentinel():
    stub = make_stub_schema(_full("mcp_a_b"))
    assert is_stub_schema(stub)


def test_is_stub_schema_rejects_full_tool():
    full = _full("mcp_zero_arg", "Real zero-arg tool", params={"type": "object", "properties": {}})
    assert not is_stub_schema(full)


def test_is_stub_schema_rejects_builtin():
    assert not is_stub_schema(_full("terminal"))


def test_mix_passes_builtins_unchanged():
    tools = [_full("terminal"), _full("read_file"), _full("mcp_x_y")]
    out = mix_full_and_stubs(tools, promoted_names=set())
    # First two unchanged
    assert out[0] == tools[0]
    assert out[1] == tools[1]
    # Third is stubbed
    assert is_stub_schema(out[2])


def test_mix_keeps_promoted_full():
    tools = [_full("mcp_a_one"), _full("mcp_a_two"), _full("mcp_b_three")]
    out = mix_full_and_stubs(tools, promoted_names={"mcp_a_one"})
    # mcp_a_one stays full; others stubbed
    assert not is_stub_schema(out[0])
    assert is_stub_schema(out[1])
    assert is_stub_schema(out[2])


def test_mix_respects_lazy_servers_filter():
    tools = [_full("mcp_trek_x"), _full("mcp_gmail_send")]
    # Only trek is in the lazy set → gmail stays full
    out = mix_full_and_stubs(tools, promoted_names=set(), lazy_servers={"trek"})
    assert is_stub_schema(out[0])  # mcp_trek_x stubbed
    assert not is_stub_schema(out[1])  # mcp_gmail_send untouched


def test_mix_with_empty_lazy_servers_stubs_all_mcp():
    tools = [_full("mcp_a_one"), _full("mcp_b_two")]
    out = mix_full_and_stubs(tools, promoted_names=set(), lazy_servers=None)
    # lazy_servers=None means "stub all MCP tools"
    assert all(is_stub_schema(t) for t in out)


def test_mix_handles_promoted_frozenset():
    tools = [_full("mcp_x_one")]
    out = mix_full_and_stubs(tools, promoted_names=frozenset({"mcp_x_one"}))
    assert not is_stub_schema(out[0])


def test_mix_returns_new_list():
    tools = [_full("mcp_x_y")]
    out = mix_full_and_stubs(tools, promoted_names=set())
    # Original list untouched.
    assert tools[0]["function"]["name"] == "mcp_x_y"
    assert "[LAZY]" not in tools[0]["function"]["description"]
    # Out list has the stub.
    assert is_stub_schema(out[0])


def test_lazy_servers_handles_dashes_and_dots():
    # Server names get sanitized to underscores in MCP tool names.
    tools = [_full("mcp_my_server_x_y")]
    out = mix_full_and_stubs(tools, promoted_names=set(), lazy_servers={"my-server-x"})
    assert is_stub_schema(out[0])


def test_make_stub_empty_description_is_safe():
    stub = make_stub_schema(_full("mcp_a_b", ""))
    assert "description" in stub["function"]
    # Shouldn't crash; description is just "[LAZY]" (trimmed)
    assert stub["function"]["description"].startswith("[LAZY]")
