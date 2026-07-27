"""Regression tests for MCP tool-name resolution (#168).

Models request tools using MCP SDK native naming (``mcp__server__tool``,
double underscores). Hermes registers them as ``mcp_server_tool`` (single
underscores). Without bridging, ``load_mcp_tools`` rejects every request
and the agent loops indefinitely.
"""
import json

import pytest

from plugins.mcp_lazy.meta_tool import handler as meta_tool_handler
from plugins.mcp_lazy.pool import _reset_for_tests, get_pool
from plugins.mcp_lazy.promote import promote_tools, resolve_tool_name
from plugins.mcp_lazy.stubs import _extract_server, _server_in_set


class FakeAgent:
    def __init__(self, session_id, tool_names):
        self.session_id = session_id
        self.valid_tool_names = set(tool_names)


@pytest.fixture(autouse=True)
def clean_pools():
    _reset_for_tests()
    yield
    _reset_for_tests()


# ----------------------------------------------------------- resolve_tool_name

def test_exact_match_passes_through():
    valid = {"mcp_lifeplan42_list_inbox", "read_file"}
    assert resolve_tool_name("mcp_lifeplan42_list_inbox", valid) == "mcp_lifeplan42_list_inbox"
    assert resolve_tool_name("read_file", valid) == "read_file"


def test_double_underscore_collapsed_to_single():
    valid = {"mcp_lifeplan42_list_inbox"}
    assert resolve_tool_name("mcp__lifeplan42__list_inbox", valid) == "mcp_lifeplan42_list_inbox"


def test_unknown_name_returns_unchanged():
    valid = {"mcp_lifeplan42_list_inbox"}
    assert resolve_tool_name("mcp_totally_fake_tool", valid) == "mcp_totally_fake_tool"


def test_empty_valid_returns_unchanged():
    assert resolve_tool_name("mcp__server__tool", set()) == "mcp__server__tool"


def test_underscore_in_server_name_resolves():
    valid = {"mcp_my_server_do_thing"}
    assert resolve_tool_name("mcp__my_server__do_thing", valid) == "mcp_my_server_do_thing"


def test_multiple_double_underscores_all_collapsed():
    valid = {"mcp_a_b_c"}
    assert resolve_tool_name("mcp__a__b__c", valid) == "mcp_a_b_c"


# ----------------------------------------------------------- meta_tool handler

async def _call_handler(agent, tool_names):
    result = await meta_tool_handler(
        args={"tool_names": tool_names},
        _agent=agent,
    )
    return json.loads(result)


@pytest.mark.asyncio
async def test_handler_promotes_mcp_native_names():
    """The exact scenario from #168: model requests mcp__server__tool."""
    agent = FakeAgent("sess-168", ["mcp_lifeplan42_list_inbox", "mcp_lifeplan42_list_documents"])
    result = await _call_handler(agent, ["mcp__lifeplan42__list_inbox"])

    assert result["ok"] is True
    assert result["promoted"] == ["mcp_lifeplan42_list_inbox"]
    assert result["rejected"] == []


@pytest.mark.asyncio
async def test_handler_mixed_native_and_correct_names():
    agent = FakeAgent("sess-mix", ["mcp_lifeplan42_list_inbox", "mcp_dart_get_task"])
    result = await _call_handler(agent, [
        "mcp__lifeplan42__list_inbox",
        "mcp_dart_get_task",
    ])

    assert result["promoted"] == ["mcp_lifeplan42_list_inbox", "mcp_dart_get_task"]
    assert result["rejected"] == []


@pytest.mark.asyncio
async def test_handler_truly_unknown_still_rejected():
    agent = FakeAgent("sess-rej", ["mcp_lifeplan42_list_inbox"])
    result = await _call_handler(agent, [
        "mcp__lifeplan42__list_inbox",
        "mcp_hallucinated_tool",
    ])

    assert result["promoted"] == ["mcp_lifeplan42_list_inbox"]
    assert "mcp_hallucinated_tool" in result["rejected"]


@pytest.mark.asyncio
async def test_promotion_actually_persists_in_pool():
    agent = FakeAgent("sess-pool", ["mcp_lifeplan42_list_inbox"])
    await _call_handler(agent, ["mcp__lifeplan42__list_inbox"])

    pool = get_pool("sess-pool")
    assert "mcp_lifeplan42_list_inbox" in pool.snapshot()


# ------------------------------------------------- server parsing (0.19 naming)
# 0.19 core registers ``mcp__<server>__<tool>`` (tools/mcp_tool.py
# MCP_TOOL_NAME_PREFIX). ``MCP_PREFIX`` here is still ``mcp_``, so before the
# fix the remainder kept a stray leading ``_`` and every server-scoped check
# (stub filtering, per-server token counts, eviction) silently matched nothing.

@pytest.mark.parametrize("tool_name", [
    "mcp__serena__find_symbol",   # 0.19 registry form
    "mcp_serena_find_symbol",     # legacy form
])
def test_server_parsing_handles_both_naming_conventions(tool_name):
    assert _server_in_set(tool_name, {"serena"}) is True
    assert _extract_server(tool_name, None) == "serena"
    assert _extract_server(tool_name, {"serena"}) == "serena"


def test_server_parsing_rejects_other_servers():
    assert _server_in_set("mcp__other__tool", {"serena"}) is False
    assert _extract_server("mcp__other__tool", {"serena"}) is None


def test_server_parsing_keeps_longest_match_and_sanitization():
    # hyphens sanitize to underscores; longest match still wins (EDGE #2)
    assert _server_in_set("mcp__my_server__tool", {"my-server"}) is True
    assert _extract_server(
        "mcp__my_tool_v2__create", {"my_tool", "my_tool_v2"}
    ) == "my_tool_v2"


def test_promote_tools_resolves_double_underscore_native_names():
    agent = FakeAgent("sess-promote-direct", ["mcp_lifeplan42_list_inbox"])
    promoted = promote_tools(agent, ["mcp__lifeplan42__list_inbox"])
    assert promoted == ["mcp_lifeplan42_list_inbox"]
    pool = get_pool("sess-promote-direct")
    assert "mcp_lifeplan42_list_inbox" in pool.snapshot()
