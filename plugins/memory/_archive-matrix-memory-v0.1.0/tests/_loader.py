from __future__ import annotations

import importlib.machinery
import importlib.util
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


def load_package():
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


def load_submodule(name: str):
    load_package()
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
