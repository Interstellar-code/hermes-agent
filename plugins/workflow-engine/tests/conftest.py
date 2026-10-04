"""
conftest.py — make `plugins.workflow_engine` importable.

The plugin directory is named `workflow-engine` (hyphen, per Hermes convention)
but Python module names cannot contain hyphens.  This conftest registers the
package under the underscore alias before any test module is collected.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Repo root = four levels up from this file
#   plugins/workflow-engine/tests/conftest.py
_PLUGIN_DIR = Path(__file__).resolve().parent.parent          # plugins/workflow-engine/
_PLUGINS_DIR = _PLUGIN_DIR.parent                             # plugins/
_REPO_ROOT = _PLUGINS_DIR.parent                              # repo root

# Ensure repo root is on sys.path so `plugins` namespace resolves.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Runtime modules use top-level imports like ``from engine.facade import ...``
# when loaded from the hyphenated plugin directory.  Add the plugin root so
# those imports resolve under direct pytest collection too.
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# Register `plugins.workflow_engine` → the hyphenated directory.
def _register(dotted: str, path: Path) -> None:
    if dotted in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        dotted,
        path / "__init__.py",
        submodule_search_locations=[str(path)],
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]


_register("plugins.workflow_engine", _PLUGIN_DIR)
_register("plugins.workflow_engine.dashboard", _PLUGIN_DIR / "dashboard")
_register("plugins.workflow_engine.engine", _PLUGIN_DIR / "engine")

# monkeypatch.setattr resolves "plugins.workflow_engine.X" by walking
# getattr chains. Ensure the attribute is set on the plugins namespace too.
import plugins as _plugins_mod  # noqa: E402
_plugins_mod.workflow_engine = sys.modules["plugins.workflow_engine"]


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_host_llm():
    """register() tests call set_llm, which sets engine.HOST_LLM process-wide."""
    import engine as engine_pkg
    prev = getattr(engine_pkg, "HOST_LLM", None)
    yield
    engine_pkg.HOST_LLM = prev


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """Default DB / migrate lock / manifest resolve under HERMES_HOME; never
    let any test (or spawned worker, which inherits env) touch ~/.hermes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
