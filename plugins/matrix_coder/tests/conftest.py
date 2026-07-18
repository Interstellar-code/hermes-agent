"""Shared fixtures for the matrix_coder test suite.

The Phase 1 config overlay reads the real ``~/.hermes/profiles/<active>/config.yaml``
when no monkeypatch is in place, so tests that don't explicitly stub the config
will pick up whatever the operator has set in their live profile.  This
``autouse`` fixture forces the audit-mirror to ON for the **lifecycle** tests
so the existing assertions still exercise the "audit enabled" path.

The ``test_config.py`` tests have their own per-test monkeypatching of
``_read_profile_overlay`` / ``_defaults`` / ``_active_profile_name`` and don't
need this autouse — and applying it would actually break their assertions by
forcing the overlay to None.  They are excluded by module name.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PLUGINS_ROOT = Path(__file__).resolve().parents[2]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

# Test modules that explicitly test config-overlay behavior.  We skip the
# autouse override for these so their per-test monkeypatching takes effect.
_CONFIG_TEST_MODULES = {"test_config.py"}


@pytest.fixture(autouse=True)
def _force_kanban_audit_enabled(request, monkeypatch):
    """Force ``kanban_audit.is_enabled()`` to return True for lifecycle tests.

    Skipped for tests in ``test_config.py`` because those tests have their own
    per-test monkeypatching of the config helpers and rely on real defaults
    being observable.
    """
    # Detect "we're in a config-overlay test" by the module's __file__.
    test_file = getattr(request.module, "__file__", "") or ""
    if any(test_file.endswith(name) for name in _CONFIG_TEST_MODULES):
        yield
        return

    try:
        from matrix_coder.core import config as _config
        from matrix_coder.core import kanban_audit

        # Force load_config to return the defaults dict (not a deep-merge
        # result) so lifecycle tests see the "audit enabled" world.
        monkeypatch.setattr(_config, "load_config", _config._defaults)

        # is_enabled() = config["KANBAN_AUDIT_ENABLED"] AND _kb is not None.
        # We preserve the original "backend missing → disabled" semantics so
        # ``test_disabled_backend_open_returns_none_close_noop`` (which sets
        # _kb=None) still passes.  Only the config half is forced to True.
        def _is_enabled_forced():
            return (
                _config._defaults().get("KANBAN_AUDIT_ENABLED", False)
                and kanban_audit._kb is not None
            )

        monkeypatch.setattr(kanban_audit, "is_enabled", _is_enabled_forced)
    except Exception:
        pass
    yield
