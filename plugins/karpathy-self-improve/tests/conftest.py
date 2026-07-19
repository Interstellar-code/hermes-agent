"""
conftest.py — make `plugins.karpathy_self_improve` importable.

The plugin directory is named `karpathy-self-improve` (hyphen, per Hermes
convention) but Python module names cannot contain hyphens.  This conftest
registers the package under the underscore alias before any test module is
collected.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent   # plugins/karpathy-self-improve/
_PLUGINS_DIR = _PLUGIN_DIR.parent                      # plugins/
_REPO_ROOT = _PLUGINS_DIR.parent                       # repo root

# Ensure repo root and plugin dir are on sys.path so both top-level absolute
# imports (_db, _metrics, daemon) and the `plugins` namespace resolve.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _register_alias(dotted: str, path: Path) -> None:
    """Register *path* as a Python package under *dotted* name."""
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


_register_alias("plugins.karpathy_self_improve", _PLUGIN_DIR)

# Ensure `plugins` namespace has the attribute so monkeypatch paths resolve.
import plugins as _plugins_ns  # noqa: E402
_plugins_ns.karpathy_self_improve = sys.modules["plugins.karpathy_self_improve"]


# ---------------------------------------------------------------------------
# Profile-root patcher — used by git-ratchet and API lifecycle tests.
#
# Tests pass tmp_path directories as profile_root, which are not under the
# real ~/.hermes/profiles.  This fixture patches _git_ratchet._PROFILES_ROOT
# to tmp_path so _assert_profile_root passes in the test environment.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


@pytest.fixture()
def patch_profiles_root(tmp_path, monkeypatch):
    """Monkeypatch _git_ratchet._PROFILES_ROOT to tmp_path for containment checks."""
    import _git_ratchet
    monkeypatch.setattr(_git_ratchet, "_PROFILES_ROOT", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Default model wiring — judge_model has no hardcoded default (#172): a wrong
# literal would silently fail every eval. Tests that only care about profile
# routing / daemon scheduling (not model resolution itself) would otherwise
# trip the new fail-fast ValueError. Give them a sane default here; tests
# that specifically exercise model resolution (test_propose_wiring.py) patch
# `_wiring._load_models` themselves, which overrides this for their duration.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _default_model_wiring(monkeypatch):
    import _wiring
    monkeypatch.setattr(_wiring, "_load_models", lambda: ("auto", "gpt-5.4"))
