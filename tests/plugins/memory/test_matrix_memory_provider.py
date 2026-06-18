"""Tests for the Matrix Memory memory provider plugin."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "memory" / "matrix-memory"
_PKG_NAME = "hermes_plugins.matrix_memory"


def _ensure_parent_namespace() -> None:
    if "hermes_plugins" not in sys.modules:
        parent = importlib.util.module_from_spec(
            importlib.machinery.ModuleSpec("hermes_plugins", loader=None, is_package=True)
        )
        parent.__path__ = []
        sys.modules["hermes_plugins"] = parent


def _load_package():
    _ensure_parent_namespace()
    module = sys.modules.get(_PKG_NAME)
    if module is not None and getattr(module, "__file__", None):
        return module
    spec = importlib.util.spec_from_file_location(
        _PKG_NAME,
        _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PKG_NAME] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_submodule(name: str):
    _load_package()
    fullname = f"{_PKG_NAME}.{name}"
    module = sys.modules.get(fullname)
    if module is not None and getattr(module, "__file__", None):
        return module
    spec = importlib.util.spec_from_file_location(fullname, _PLUGIN_DIR / (name.replace(".", "/") + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


init_mod = _load_package()
provider_mod = _load_submodule("provider")
MatrixMemoryProvider = provider_mod.MatrixMemoryProvider


@pytest.fixture
def provider(tmp_path):
    p = MatrixMemoryProvider()
    p.initialize("session-1", hermes_home=str(tmp_path), mode="chat", platform="cli")
    return p


def test_register_calls_register_memory_provider():
    ctx = MagicMock()
    init_mod.register(ctx)
    ctx.register_memory_provider.assert_called_once()
    arg = ctx.register_memory_provider.call_args[0][0]
    assert isinstance(arg, MatrixMemoryProvider)


def test_discover_and_load_provider():
    from plugins.memory import discover_memory_providers, load_memory_provider

    providers = {name: (desc, available) for name, desc, available in discover_memory_providers()}
    assert "matrix-memory" in providers
    loaded = load_memory_provider("matrix-memory")
    assert loaded is not None
    assert loaded.name == "matrix-memory"


def test_provider_exposes_chat_and_base_tools(provider):
    names = {schema["name"] for schema in provider.get_tool_schemas()}
    assert {"memory_recall", "memory_note", "memory_ingest", "memory_forget", "memory_status"}.issubset(names)
    assert {"memory_show", "memory_audit", "memory_trace"}.issubset(names)


def test_provider_routes_tool_calls(provider):
    result = json.loads(
        provider.handle_tool_call(
            "memory_note",
            {
                "target": "wiki",
                "title": "Atlas Status",
                "content": "Atlas rollout is green.",
                "folder": "queries",
                "dry_run": False,
            },
        )
    )
    assert result["success"] is True
    recall = json.loads(provider.handle_tool_call("memory_recall", {"query": "Atlas rollout"}))
    assert recall["success"] is True
    assert recall["summary"]["tier3_hits"] >= 1
