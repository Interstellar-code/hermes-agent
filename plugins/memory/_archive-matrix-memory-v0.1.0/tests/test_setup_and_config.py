from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

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


provider_mod = _load_submodule("provider")
MatrixMemoryProvider = provider_mod.MatrixMemoryProvider


def test_get_config_schema_contains_expected_keys():
    provider = MatrixMemoryProvider()
    keys = {field["key"] for field in provider.get_config_schema()}
    assert keys == {"wiki_root", "stale_after_days", "chunk_chars"}


def test_save_config_writes_json(tmp_path: Path):
    provider = MatrixMemoryProvider()
    provider.save_config({"chunk_chars": "1200"}, str(tmp_path))
    config_path = tmp_path / "matrix-memory" / "matrix_memory.json"
    assert config_path.exists()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["chunk_chars"] == "1200"
