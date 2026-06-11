"""test_register_contract.py — verify register(ctx) contract for hermes-switch-ui.

Checks:
 - exactly 1 pre_llm_call hook registered
 - exactly 2 tools: switchui_info and switchui_status
 - nudge text is non-empty and single paragraph (no double-newline)
 - register_skill called when ctx has the method, capturing name='switchui' + existing SKILL.md
 - registration succeeds when ctx lacks register_skill (hasattr guard)
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Helpers: load __init__.py via spec_from_file_location
# ---------------------------------------------------------------------------

_PLUGIN_DIR = Path(__file__).resolve().parent.parent  # plugins/hermes-switch-ui/


def _load_plugin() -> Any:
    """Load hermes-switch-ui __init__.py fresh via spec_from_file_location."""
    init_path = _PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_switch_ui_plugin", init_path
    )
    mod = importlib.util.module_from_spec(spec)
    # Inject plugin dir into sys.path so _knowledge etc. resolve
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# FakeCtx — mirrors karpathy harness conventions
# ---------------------------------------------------------------------------

@dataclass
class FakeCtx:
    hooks: List[Dict[str, Any]] = field(default_factory=list)
    tools: List[Dict[str, Any]] = field(default_factory=list)
    skills: List[Dict[str, Any]] = field(default_factory=list)

    def register_hook(self, hook_name: str, callback: Callable) -> None:
        self.hooks.append({"hook": hook_name, "callback": callback})

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        description: str = "",
        emoji: str = "",
        **kwargs: Any,
    ) -> None:
        self.tools.append({"name": name, "toolset": toolset, "handler": handler})

    def register_skill(
        self,
        name: str,
        path: Path,
        description: str = "",
    ) -> None:
        self.skills.append({"name": name, "path": path, "description": description})


@dataclass
class FakeCtxNoSkill:
    """FakeCtx that deliberately lacks register_skill (older PluginContext)."""
    hooks: List[Dict[str, Any]] = field(default_factory=list)
    tools: List[Dict[str, Any]] = field(default_factory=list)

    def register_hook(self, hook_name: str, callback: Callable) -> None:
        self.hooks.append({"hook": hook_name, "callback": callback})

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        description: str = "",
        emoji: str = "",
        **kwargs: Any,
    ) -> None:
        self.tools.append({"name": name, "toolset": toolset, "handler": handler})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_register_one_hook():
    """register() must register exactly 1 pre_llm_call hook."""
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)

    hook_names = [h["hook"] for h in ctx.hooks]
    assert hook_names == ["pre_llm_call"], (
        f"Expected exactly ['pre_llm_call'], got: {hook_names}"
    )


def test_register_two_tools():
    """register() must register exactly 2 tools: switchui_info and switchui_status."""
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)

    tool_names = [t["name"] for t in ctx.tools]
    assert len(ctx.tools) == 2, f"Expected 2 tools, got {len(ctx.tools)}: {tool_names}"
    assert "switchui_info" in tool_names, f"switchui_info missing from {tool_names}"
    assert "switchui_status" in tool_names, f"switchui_status missing from {tool_names}"


def test_register_tool_handlers_are_callable():
    """Each registered tool must have a callable handler."""
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)

    for t in ctx.tools:
        assert callable(t["handler"]), f"Tool {t['name']} handler is not callable"


def test_nudge_non_empty_single_paragraph():
    """_NUDGE must be non-empty and have no double-newline (single paragraph)."""
    plugin = _load_plugin()
    nudge = plugin._NUDGE
    assert nudge and nudge.strip(), "_NUDGE must not be empty"
    assert "\n\n" not in nudge, (
        "_NUDGE must be a single paragraph (no double-newline found)"
    )


def test_nudge_returned_by_hook_first_call():
    """Hook returns {'context': <nudge>} on first call for a session."""
    plugin = _load_plugin()
    # Clear nudge state so this test is independent of load order
    plugin._nudged_sessions.discard("test-session-abc")

    result = plugin._pre_llm_call(session_id="test-session-abc")
    assert result is not None, "First hook call must return a dict"
    assert "context" in result, f"Expected 'context' key, got: {result}"
    assert result["context"] == plugin._NUDGE


def test_nudge_suppressed_on_second_call():
    """Hook returns None on second call for the same session (once-per-session)."""
    plugin = _load_plugin()
    plugin._nudged_sessions.discard("test-session-xyz")

    plugin._pre_llm_call(session_id="test-session-xyz")
    result2 = plugin._pre_llm_call(session_id="test-session-xyz")
    assert result2 is None, "Second hook call for same session must return None"


def test_register_skill_captured():
    """When ctx has register_skill, one skill named 'switchui' must be registered."""
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)

    assert len(ctx.skills) == 1, f"Expected 1 skill, got {len(ctx.skills)}: {ctx.skills}"
    skill = ctx.skills[0]
    assert skill["name"] == "switchui", f"Expected skill name 'switchui', got: {skill['name']}"


def test_register_skill_path_exists():
    """The skill path passed to register_skill must point to an existing SKILL.md."""
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)

    assert ctx.skills, "No skill registered"
    skill_path = ctx.skills[0]["path"]
    assert Path(skill_path).exists(), f"SKILL.md path does not exist: {skill_path}"
    assert Path(skill_path).name == "SKILL.md", (
        f"Expected SKILL.md filename, got: {Path(skill_path).name}"
    )


def test_register_succeeds_without_register_skill():
    """register() must not raise when ctx lacks register_skill (hasattr guard)."""
    plugin = _load_plugin()
    ctx = FakeCtxNoSkill()
    # Must not raise
    plugin.register(ctx)
    # Core registrations still happen
    assert len(ctx.hooks) == 1
    assert len(ctx.tools) == 2
