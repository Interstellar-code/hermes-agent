"""test_register_contract.py — verify register(ctx) + hook contract for personas.

Checks:
 - exactly 1 pre_llm_call hook
 - exactly 3 tools: persona_list / persona_get / persona_apply
 - handlers are callable and accept the whole-args-dict-as-first-positional shape
 - pre_llm_call returns None when no persona_ref is set (cache-warm common path)
 - pre_llm_call returns a TRUSTED dict (target="developer") when a ref resolves
 - registration succeeds when ctx lacks register_skill (hasattr guard)
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

_PLUGIN_DIR = Path(__file__).resolve().parent.parent  # plugins/personas/


def _load_plugin() -> Any:
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    spec = importlib.util.spec_from_file_location("personas_plugin", _PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@dataclass
class FakeCtx:
    hooks: List[Dict[str, Any]] = field(default_factory=list)
    tools: List[Dict[str, Any]] = field(default_factory=list)
    skills: List[Dict[str, Any]] = field(default_factory=list)

    def register_hook(self, hook_name: str, callback: Callable) -> None:
        self.hooks.append({"hook": hook_name, "callback": callback})

    def register_tool(self, name: str, toolset: str, schema: dict, handler: Callable,
                      description: str = "", emoji: str = "", **kwargs: Any) -> None:
        self.tools.append({"name": name, "toolset": toolset, "schema": schema, "handler": handler})

    def register_skill(self, name: str, path: Path, description: str = "") -> None:
        self.skills.append({"name": name, "path": path, "description": description})


@dataclass
class FakeCtxNoSkill:
    hooks: List[Dict[str, Any]] = field(default_factory=list)
    tools: List[Dict[str, Any]] = field(default_factory=list)

    def register_hook(self, hook_name: str, callback: Callable) -> None:
        self.hooks.append({"hook": hook_name, "callback": callback})

    def register_tool(self, name: str, toolset: str, schema: dict, handler: Callable,
                      description: str = "", emoji: str = "", **kwargs: Any) -> None:
        self.tools.append({"name": name, "toolset": toolset, "handler": handler})


def test_register_one_hook():
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    assert [h["hook"] for h in ctx.hooks] == ["pre_llm_call"]


def test_register_three_tools():
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    names = sorted(t["name"] for t in ctx.tools)
    assert names == ["persona_apply", "persona_get", "persona_list"]
    assert all(t["toolset"] == "personas" for t in ctx.tools)


def test_handlers_callable():
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    for t in ctx.tools:
        assert callable(t["handler"])


def test_persona_list_handler_shape():
    """Handler takes the whole args dict as first positional and returns personas."""
    plugin = _load_plugin()
    out = plugin._tool_persona_list({})
    assert "personas" in out and out["count"] == 20


def test_persona_get_handler():
    plugin = _load_plugin()
    out = plugin._tool_persona_get({"persona_id": "engineering-security-engineer"})
    assert "persona" in out and out["persona"]["system_prompt"].strip()
    miss = plugin._tool_persona_get({"persona_id": "nope"})
    assert "error" in miss


def test_persona_apply_handler():
    plugin = _load_plugin()
    out = plugin._tool_persona_apply({"persona_id": "engineering-security-engineer"})
    assert out["target"] == "delegate"
    assert "Active persona lens" in out["overlay"]


def test_hook_none_when_no_persona_ref(monkeypatch):
    """Common path: no persona_ref -> None (keeps cached prefix warm)."""
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "_read_persona_ref", lambda: None)
    assert plugin._pre_llm_call(session_id="s1") is None


def test_hook_trusted_target_when_ref_set(monkeypatch):
    """#140-safe: resolved persona overlay must use target='developer'."""
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "_read_persona_ref", lambda: "engineering-security-engineer")
    result = plugin._pre_llm_call(session_id="s2")
    assert result is not None
    assert result["target"] == "developer", "persona overlay MUST be trusted-tier, never user_message"
    assert result["context"].strip()


def test_hook_unknown_ref_returns_none(monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "_read_persona_ref", lambda: "does-not-exist")
    assert plugin._pre_llm_call(session_id="s3") is None


def test_register_succeeds_without_register_skill():
    plugin = _load_plugin()
    ctx = FakeCtxNoSkill()
    plugin.register(ctx)  # must not raise
    assert len(ctx.hooks) == 1
    assert len(ctx.tools) == 3
