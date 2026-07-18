"""Tests for the Phase 1 profile-aware ``core/config.py:load_config()``.

The original Phase 0 ``load_config()`` returned hardcoded defaults and
read one env var.  Phase 1 adds a deep-merge overlay from the active
profile's ``config.yaml`` so users can toggle flags like
``KANBAN_AUDIT_ENABLED`` without editing the plugin source.

These tests monkeypatch :func:`_read_profile_overlay` (the seam
documented in ``config.py``) to avoid touching the filesystem and to
keep the suite hermetic across test runners.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

# Same package-path shim as the other tests in this directory.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

from matrix_coder.core import config  # noqa: E402


def _reload_config():
    """Re-import config module to pick up env-var changes between tests."""
    return importlib.reload(config)


def test_defaults_when_no_overlay(monkeypatch):
    """Without an overlay, all defaults come through unchanged."""
    monkeypatch.setattr(config, "_read_profile_overlay", lambda: {})
    # Make sure no env var is set
    monkeypatch.delenv("MATRIX_CODER_IMPLICIT_ROUTING", raising=False)

    cfg = config.load_config()
    assert cfg["enabled"] is True
    assert cfg["KANBAN_AUDIT_ENABLED"] is True
    assert cfg["default_verdict"] == "MATRIX"
    assert cfg["single_writer_per_file"] is True
    # Env var unset → default behaviour (routing enabled)
    assert cfg["implicit_routing_enabled"] is True


def test_overlay_overrides_scalar(monkeypatch):
    """A scalar override in the profile config wins over the default."""
    monkeypatch.setattr(
        config,
        "_read_profile_overlay",
        lambda: {"KANBAN_AUDIT_ENABLED": False},
    )
    monkeypatch.delenv("MATRIX_CODER_IMPLICIT_ROUTING", raising=False)

    cfg = config.load_config()
    assert cfg["KANBAN_AUDIT_ENABLED"] is False
    # Other keys are untouched
    assert cfg["enabled"] is True
    assert cfg["default_verdict"] == "MATRIX"


def test_overlay_does_not_mutate_defaults(monkeypatch):
    """A loaded config must not be a reference to the defaults dict."""
    monkeypatch.setattr(
        config,
        "_read_profile_overlay",
        lambda: {"dispatch_category_model": {"deep": "gpt-x", "quick": "gpt-y"}},
    )
    monkeypatch.delenv("MATRIX_CODER_IMPLICIT_ROUTING", raising=False)

    cfg1 = config.load_config()
    cfg2 = config.load_config()
    # Each call returns a fresh dict
    assert cfg1 is not cfg2
    # Mutating one call's result must not affect the defaults
    cfg1["KANBAN_AUDIT_ENABLED"] = "mutated"
    cfg3 = config.load_config()
    assert cfg3["KANBAN_AUDIT_ENABLED"] is True


def test_overlay_recursive_merge_for_nested_dict(monkeypatch):
    """A nested dict in the overlay merges recursively with the default."""
    monkeypatch.setattr(
        config,
        "_read_profile_overlay",
        lambda: {"dispatch_category_model": {"deep": "gpt-x"}},
    )
    monkeypatch.delenv("MATRIX_CODER_IMPLICIT_ROUTING", raising=False)

    cfg = config.load_config()
    # The overlay replaced the 'deep' key only; 'quick' still inherits default
    assert cfg["dispatch_category_model"]["deep"] == "gpt-x"
    assert cfg["dispatch_category_model"]["quick"] is None


def test_env_var_still_overrides_implicit_routing(monkeypatch):
    """The MATRIX_CODER_IMPLICIT_ROUTING env var must always win for that flag,
    even if the profile overlay sets implicit_routing_enabled to True."""
    monkeypatch.setattr(
        config,
        "_read_profile_overlay",
        lambda: {"implicit_routing_enabled": True},
    )
    monkeypatch.setenv("MATRIX_CODER_IMPLICIT_ROUTING", "0")

    cfg = config.load_config()
    # Env var wins
    assert cfg["implicit_routing_enabled"] is False
    # Other overlay values still apply
    # (here the overlay was just implicit_routing_enabled, nothing else to check)


def test_env_var_enable_value(monkeypatch):
    """Anything other than '0' in MATRIX_CODER_IMPLICIT_ROUTING enables routing."""
    monkeypatch.setattr(config, "_read_profile_overlay", lambda: {})
    monkeypatch.setenv("MATRIX_CODER_IMPLICIT_ROUTING", "1")

    cfg = config.load_config()
    assert cfg["implicit_routing_enabled"] is True


def test_missing_profile_file_returns_defaults(monkeypatch, tmp_path):
    """When the active profile config doesn't exist, the function must
    return defaults without raising — a missing file is not an error."""
    # Point get_hermes_home at an empty tmp dir so the profile path is missing.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("MATRIX_CODER_IMPLICIT_ROUTING", raising=False)
    # The import inside _profile_config_path caches hermes_constants.get_hermes_home
    # at import time, so we patch the module-level function it calls.
    import hermes_constants  # noqa: E402
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        hermes_constants, "get_hermes_home_override", lambda: None
    )

    cfg = config.load_config()
    assert cfg["KANBAN_AUDIT_ENABLED"] is True
    assert cfg["enabled"] is True


def test_active_profile_name_resolution(monkeypatch, tmp_path):
    """The active profile comes from HERMES_PROFILE → active_profile file → default."""
    # 1. HERMES_PROFILE wins
    monkeypatch.setenv("HERMES_PROFILE", "from-env")
    assert config._active_profile_name() == "from-env"

    # 2. active_profile marker file wins when env var is empty
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    import hermes_constants  # noqa: E402
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        hermes_constants, "get_hermes_home_override", lambda: None
    )
    marker = tmp_path / "active_profile"
    marker.write_text("from-marker\n", encoding="utf-8")
    assert config._active_profile_name() == "from-marker"

    # 3. Fallback to "default" when neither is set
    marker.unlink()
    assert config._active_profile_name() == "default"


def test_disabled_kanban_audit_via_overlay(monkeypatch):
    """End-to-end: a profile overlay turning KANBAN_AUDIT_ENABLED off must
    make ``kanban_audit.is_enabled()`` return False even when the kanban
    backend is available."""
    monkeypatch.setattr(
        config,
        "_read_profile_overlay",
        lambda: {"KANBAN_AUDIT_ENABLED": False},
    )
    monkeypatch.delenv("MATRIX_CODER_IMPLICIT_ROUTING", raising=False)

    # Re-evaluate is_enabled() with the patched config.
    from matrix_coder.core import kanban_audit  # noqa: E402

    # Backend is not None here (the real import succeeded) — but the config
    # gate should now be off.
    assert kanban_audit._kb is not None  # sanity check on the import
    assert kanban_audit.is_enabled() is False


def test_profile_scoped_hermes_home_yields_correct_config_path(
    monkeypatch, tmp_path
):
    """Regression test for the silent double-prepend bug.

    When the operator runs in a profile-scoped shell (the default for the
    gateway), ``HERMES_HOME`` is already the profile directory:

        HERMES_HOME=/Users/.../.hermes/profiles/hermes-switch

    The path helper must resolve this to the profile's config.yaml
    (``.../hermes-switch/config.yaml``) without double-prepending
    ``profiles/<name>/``.  The earlier implementation built the path manually
    from ``get_hermes_home()`` and ``_active_profile_name()`` and produced
    ``.../hermes-switch/profiles/hermes-switch/config.yaml`` — a non-existent
    path that silently fell through to the defaults, so operators who set
    ``matrix_coder.KANBAN_AUDIT_ENABLED: false`` in their config.yaml saw
    the audit mirror keep running.  See the bug+fix comment on
    Interstellar-code/hermes-agent issue #142.
    """
    profile_dir = tmp_path / "hermes-switch"
    profile_dir.mkdir()
    (profile_dir / "config.yaml").write_text(
        "matrix_coder:\n  KANBAN_AUDIT_ENABLED: false\n",
        encoding="utf-8",
    )

    # Make hermes_constants.get_config_path see our fake HERMES_HOME.
    from matrix_coder.core import config  # local import for the monkeypatch target

    import sys
    fake_hc = type(sys)("hermes_constants")
    fake_hc.get_hermes_home = lambda: profile_dir
    fake_hc.get_config_path = lambda: profile_dir / "config.yaml"
    monkeypatch.setitem(sys.modules, "hermes_constants", fake_hc)

    # Also neutralize the env so resolution order is deterministic.
    monkeypatch.delenv("HERMES_PROFILE", raising=False)

    path = config._profile_config_path()
    assert path == str(profile_dir / "config.yaml"), (
        f"Profile-scoped HERMES_HOME must yield the profile config path, "
        f"not a double-prepended path. Got: {path}"
    )

    # And the overlay reader must actually read the file.
    overlay = config._read_profile_overlay()
    assert overlay == {"KANBAN_AUDIT_ENABLED": False}, (
        f"Overlay should reflect the operator's config.yaml. Got: {overlay}"
    )
