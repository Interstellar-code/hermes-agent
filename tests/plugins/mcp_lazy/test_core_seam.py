"""Guard the four core seams mcp_lazy depends on.

These live in core files, not in the plugin, so every upstream tree adoption
drops them and the plugin then loads, registers, and silently no-ops. That has
happened three times: v0.16 (fixed in 4b1608e33), v0.17 (fixed in 7849f82415,
misattributed to OAuth in #149), and v0.19.

The failure mode is invisible without this test — the plugin imports fine, the
hook registers fine, and every auto-promote path just quietly returns None.
"""
import inspect

import pytest

from hermes_cli.plugins import VALID_HOOKS, get_plugin_manager, invoke_hook


def test_transform_tools_is_a_known_hook():
    # Cosmetic on its own (register_hook stores unknown names and only warns),
    # but its absence is the signal that an adoption dropped the seam.
    assert "transform_tools" in VALID_HOOKS


def test_transform_tools_dispatches_with_the_agent_kwarg():
    seen = []
    get_plugin_manager()._hooks.setdefault("transform_tools", []).append(
        lambda **kw: seen.append(kw) or None
    )
    try:
        invoke_hook("transform_tools", tools=[{"name": "t"}], agent=object(), api_messages=[])
    finally:
        get_plugin_manager()._hooks["transform_tools"].pop()
    assert seen, "invoke_hook('transform_tools') dispatched to nothing"
    assert "agent" in seen[0], f"agent kwarg not passed through: {sorted(seen[0])}"


def test_build_api_kwargs_fires_transform_tools_after_binding_tools():
    from agent import chat_completion_helpers as cch

    src = inspect.getsource(cch.build_api_kwargs)
    assert "transform_tools" in src, "build_api_kwargs does not fire transform_tools"
    assert src.index("tools_for_api = agent.tools") < src.index("transform_tools"), \
        "transform_tools fires before tools_for_api is bound"


def test_usage_observer_registry_exists():
    # plugins/mcp_lazy/baseline_patch.py hooks into this.
    from agent import usage_pricing

    assert hasattr(usage_pricing, "register_usage_observer")
    assert hasattr(usage_pricing, "unregister_usage_observer")


def test_mcp_lazy_config_keys_validate():
    from hermes_cli.config import validate_config_structure

    src = inspect.getsource(validate_config_structure)
    assert "lazy_loading" in src and "discovery_mode" in src


def test_transform_tools_sets_the_contextvar_pre_tool_call_reads(monkeypatch):
    from plugins.mcp_lazy import hook_impl

    class _Agent:  # fails open unless lazy mode is on AND the agent has a session
        session_id = "seam-check"
        tools: list = []

    agent = _Agent()
    monkeypatch.setattr(hook_impl, "_load_config", lambda *a, **k: {"lazy_loading": True})
    hook_impl.transform_tools(tools=[{"name": "t"}], agent=agent, api_messages=[])

    assert hook_impl._current_agent_var.get(None) is agent, \
        "transform_tools did not set _current_agent_var — every auto-promote path no-ops"


def test_pre_tool_call_seam_is_wired_into_dispatch():
    # Upstream 0.19 renamed get_pre_tool_call_block_message -> resolve_pre_tool_block.
    # Checking for the old name yields a false "deleted" reading; check behaviour.
    from hermes_cli import plugins

    assert hasattr(plugins, "resolve_pre_tool_block")

    called = {}

    def _hook(**_kw):
        called["hit"] = True
        return {"action": "block", "message": "nope"}

    get_plugin_manager()._hooks.setdefault("pre_tool_call", []).append(_hook)
    try:
        msg = plugins.resolve_pre_tool_block("some_tool", {})
    finally:
        get_plugin_manager()._hooks["pre_tool_call"].pop()

    assert called.get("hit"), "resolve_pre_tool_block never invoked the pre_tool_call hook"
    assert msg == "nope", f"raw-dict block directive not honored, got {msg!r}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
