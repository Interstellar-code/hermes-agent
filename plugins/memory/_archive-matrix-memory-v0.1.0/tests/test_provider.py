from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "hermes_plugins.matrix_memory"


def _ensure_parent_namespace() -> None:
    if "hermes_plugins" not in sys.modules:
        parent = importlib.util.module_from_spec(
            importlib.machinery.ModuleSpec("hermes_plugins", loader=None, is_package=True)
        )
        parent.__path__ = []
        sys.modules["hermes_plugins"] = parent


def _load_package():
    _ensure_parent_namespace()
    module = sys.modules.get(PKG_NAME)
    if module is not None and getattr(module, "__file__", None):
        return module
    spec = importlib.util.spec_from_file_location(
        PKG_NAME,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG_NAME] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_submodule(name: str):
    _load_package()
    fullname = f"{PKG_NAME}.{name}"
    module = sys.modules.get(fullname)
    if module is not None and getattr(module, "__file__", None):
        return module
    path = ROOT / (name.replace(".", "/") + ".py")
    spec = importlib.util.spec_from_file_location(fullname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


init_mod = _load_package()
provider_mod = _load_submodule("provider")
MatrixMemoryProvider = provider_mod.MatrixMemoryProvider


def _init_provider(tmp_path: Path, *, mode: str = "normal") -> MatrixMemoryProvider:
    provider = MatrixMemoryProvider()
    provider.initialize("session-1", hermes_home=str(tmp_path), mode=mode, platform="cli")
    return provider


def test_register_calls_register_memory_provider_and_skill():
    ctx = MagicMock()
    init_mod.register(ctx)
    ctx.register_memory_provider.assert_called_once()
    provider = ctx.register_memory_provider.call_args[0][0]
    assert isinstance(provider, MatrixMemoryProvider)
    ctx.register_skill.assert_called_once()
    assert ctx.register_skill.call_args.kwargs["name"] == "matrix-memory"


def test_register_succeeds_without_register_skill():
    class Ctx:
        def __init__(self):
            self.providers = []

        def register_memory_provider(self, provider):
            self.providers.append(provider)

    ctx = Ctx()
    init_mod.register(ctx)
    assert len(ctx.providers) == 1
    assert isinstance(ctx.providers[0], MatrixMemoryProvider)


def test_base_tool_schemas_present_in_normal_mode(tmp_path: Path):
    provider = _init_provider(tmp_path, mode="normal")
    names = {schema["name"] for schema in provider.get_tool_schemas()}
    assert names == {"memory_recall", "memory_note", "memory_ingest", "memory_forget", "memory_status"}


def test_chat_mode_adds_chat_tools(tmp_path: Path):
    provider = _init_provider(tmp_path, mode="chat")
    names = {schema["name"] for schema in provider.get_tool_schemas()}
    assert "memory_audit" in names
    assert "memory_show" in names
    assert len(names) == 14


def test_memory_note_writes_wiki_and_recall_finds_it(tmp_path: Path):
    provider = _init_provider(tmp_path)
    result = provider.memory_note(
        {
            "target": "wiki",
            "title": "Project Atlas",
            "content": "Atlas rollout depends on [[EU Team]].",
            "folder": "entities",
            "dry_run": False,
        }
    )
    assert result["success"] is True
    recall = provider.memory_recall({"query": "Atlas rollout", "top_k": 5})
    assert recall["success"] is True
    assert recall["summary"]["tier2_hits"] >= 1
    assert any(hit["path"].endswith("project-atlas.md") for hit in recall["tier3"])


def test_memory_note_dry_run_in_chat_mode(tmp_path: Path):
    provider = _init_provider(tmp_path, mode="chat")
    result = provider.memory_note({"target": "wiki", "title": "Draft Page", "content": "Preview only"})
    assert result["dry_run"] is True
    assert result["action"] == "memory_note"


def test_memory_forget_requires_confirm_token_in_chat_mode(tmp_path: Path):
    provider = _init_provider(tmp_path, mode="chat")
    page = provider.memory_note(
        {
            "target": "wiki",
            "title": "Disposable Page",
            "content": "Delete me",
            "dry_run": False,
        }
    )
    preview = provider.memory_forget({"kind": "page", "target": page["page"]})
    assert preview["requires_confirmation"] is True
    denied = provider.memory_forget({"kind": "page", "target": page["page"], "dry_run": False})
    assert denied["success"] is False
    applied = provider.memory_forget(
        {
            "kind": "page",
            "target": page["page"],
            "dry_run": False,
            "confirm_token": preview["confirm_token"],
        }
    )
    assert applied["success"] is True
    assert applied["removed"] == 1


def test_chat_only_tools_fail_outside_chat(tmp_path: Path):
    provider = _init_provider(tmp_path, mode="normal")
    payload = json.loads(provider.handle_tool_call("memory_list", {}))
    assert payload["success"] is False
    assert "requires session mode 'chat'" in payload["error"]
