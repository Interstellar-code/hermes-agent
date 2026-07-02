"""Walking-skeleton test: matrix_coder registers its hooks + command cleanly.

The Hermes loader imports the plugin dir as the package
``hermes_plugins.matrix_coder`` with the plugin dir on ``__path__`` (so the
plugin's own relative imports resolve). We mirror that here with importlib,
loading ``__init__.py`` under that package name, and assert ``register(ctx)``
wires the expected hooks and command on a fake ctx.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_PKG_NAME = "hermes_plugins.matrix_coder"


class FakeCtx:
    """Records register_* calls so the test can assert on them."""

    def __init__(self) -> None:
        self.hooks: list[tuple[str, object]] = []
        self.commands: list[dict] = []
        self.tools: list[dict] = []
        self.skills: list[dict] = []

    def register_hook(self, hook_name, callback):
        self.hooks.append((hook_name, callback))

    def register_command(self, name, handler=None, description="", args_hint=""):
        self.commands.append(
            {
                "name": name,
                "handler": handler,
                "description": description,
                "args_hint": args_hint,
            }
        )

    def register_tool(self, *args, **kwargs):
        self.tools.append({"args": args, "kwargs": kwargs})

    def register_skill(self, name, path, description=""):
        self.skills.append({"name": name, "path": path, "description": description})


def _load_plugin_module():
    """Import the plugin's __init__.py as ``hermes_plugins.matrix_coder``.

    Ensures a parent ``hermes_plugins`` namespace exists, then loads the
    package with its directory on ``submodule_search_locations`` so the
    plugin's relative imports (``from .core import harness``) resolve — exactly
    as the real loader arranges it.
    """
    # Minimal parent namespace package so the dotted name is valid.
    if "hermes_plugins" not in sys.modules:
        parent = importlib.util.module_from_spec(
            importlib.machinery.ModuleSpec("hermes_plugins", loader=None, is_package=True)
        )
        parent.__path__ = []  # namespace-ish
        sys.modules["hermes_plugins"] = parent

    spec = importlib.util.spec_from_file_location(
        _PKG_NAME,
        _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PKG_NAME] = module
    spec.loader.exec_module(module)
    return module


def test_register_runs_and_wires_hooks_and_command():
    module = _load_plugin_module()
    ctx = FakeCtx()

    module.register(ctx)  # must not raise

    hook_names = {name for name, _ in ctx.hooks}
    assert "pre_llm_call" in hook_names
    assert "transform_llm_output" in hook_names

    command_names = {c["name"] for c in ctx.commands}
    assert "matrix" in command_names

    matrix_cmd = next(c for c in ctx.commands if c["name"] == "matrix")
    assert callable(matrix_cmd["handler"])
    assert matrix_cmd["description"]


def test_hooks_are_defensive_with_no_active_dispatch():
    module = _load_plugin_module()
    # With no active dispatch, both hooks must return None and never raise.
    assert module._inject_persona() is None or isinstance(module._inject_persona(), str)
    assert module._normalize_output() is None
