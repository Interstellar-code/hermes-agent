"""Matrix Coder configuration defaults + profile overlay.

The defaults below are the source of truth when no profile override exists.
Phase 0: pure-stdlib defaults.  Phase 1 (this version): overlay any
``matrix_coder:`` section from the active profile's ``config.yaml`` on top of
the defaults, so users can toggle flags like ``KANBAN_AUDIT_ENABLED`` without
editing the plugin source.

Profile discovery mirrors the precedent in
``plugins/a2a_fleet/fleet_config.py::_legacy_profile_name``:
``HERMES_PROFILE`` env var → ``~/.hermes/active_profile`` marker file →
``"default"`` fallback.

Defensive contract: every I/O call is wrapped in try/except.  Any failure to
read the profile config (missing file, bad YAML, missing dependency) returns
the hardcoded defaults — the plugin must NEVER break the hot path because
someone misconfigured their profile.

Tests inject a fake config by monkeypatching :func:`_read_profile_overlay`.
"""

from __future__ import annotations

import copy
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Dispatch categories map to the model a specialist runs on.  ``None`` means
# "inherit the parent session's model" — the real mapping is resolved from
# Hermes config in a later phase, so these are placeholders only.
DISPATCH_CATEGORY_MODEL: Dict[str, Optional[str]] = {
    "deep": None,   # heavyweight reasoning roles (plan, review, verify)
    "quick": None,  # lightweight passes (explore, simplify)
}


def _defaults() -> Dict[str, Any]:
    """Return the hardcoded baseline config.

    Factored out so tests can compare against the baseline without parsing the
    real profile overlay.
    """
    return {
        "enabled": True,
        # Default intake behaviour: route ambiguous/complex work through the
        # matrix rather than answering directly.
        "default_verdict": "MATRIX",
        "dispatch_category_model": dict(DISPATCH_CATEGORY_MODEL),
        # Single-writer-per-file guardrail is enforced at orchestration time;
        # this flag exists so later phases can toggle the bookkeeping.
        "single_writer_per_file": True,
        # Phase 2 audit-mirror: mirror each matrix invocation as a Hermes Kanban
        # card for live observability on the Switch UI. Purely an audit layer,
        # never control flow. Set False to disable mirroring entirely.
        "KANBAN_AUDIT_ENABLED": True,
        # Phase 5 implicit routing kill-switch.  Set to False (or set the env
        # var MATRIX_CODER_IMPLICIT_ROUTING=0) to disable all implicit intent
        # routing; explicit "matrix ..." triggers are unaffected.
        "implicit_routing_enabled": True,
    }


def _active_profile_name() -> str:
    """Return the active Hermes profile name. Defensive — never raises.

    Resolution order (matches ``plugins/a2a_fleet/fleet_config.py``):
      1. ``HERMES_PROFILE`` env var (set by the CLI when a profile is active).
      2. ``<hermes_home>/active_profile`` marker file (sticky default).
      3. ``"default"`` fallback.
    """
    try:
        env_profile = os.environ.get("HERMES_PROFILE")
        if env_profile and env_profile.strip():
            return env_profile.strip()
        # Local import: hermes_constants is a top-level package, but we want
        # to keep this module loadable in the standalone test harness (which
        # stubs out get_hermes_home via sys.path).  A local import is fine
        # because it's only hit on real calls, not at import time.
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
        marker = get_hermes_home() / "active_profile"
        if marker.is_file():
            name = marker.read_text(encoding="utf-8").strip()
            if name:
                return name
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: _active_profile_name fallback: %s", exc)
    return "default"


def _profile_config_path() -> Optional[str]:
    """Return the absolute path to the active profile's config.yaml, or None.

    Returns ``None`` if HERMES_HOME can't be resolved — the caller treats
    that as "no overlay" and returns the defaults unchanged.

    Uses ``get_config_path()`` from ``hermes_constants`` because that helper
    correctly handles BOTH layouts:

      - HERMES_HOME=/Users/.../.hermes  (un-scoped)         → returns ~/.hermes/profiles/<name>/config.yaml
      - HERMES_HOME=/Users/.../.hermes/profiles/<name>     → returns ~/.hermes/profiles/<name>/config.yaml

    An earlier version of this function built the path manually from
    ``get_hermes_home()`` and ``_active_profile_name()``, which double-prepended
    ``profiles/<name>/`` in the second layout and silently returned a
    non-existent path — the overlay never loaded, and ``is_enabled()`` stayed
    True even after the operator set the flag.  See issue #142 comment
    "matrix_coder: KANBAN_AUDIT_ENABLED override silently ignored" (the bug
    that motivated this fix).
    """
    try:
        from hermes_constants import get_config_path  # type: ignore[import-not-found]
        return str(get_config_path())
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: _profile_config_path: get_config_path failed: %s", exc)
        return None


def _read_profile_overlay() -> Dict[str, Any]:
    """Read the ``matrix_coder:`` section from the active profile's config.

    Returns an empty dict on any failure (missing file, no section, bad YAML,
    missing PyYAML) — the caller overlays an empty dict, which is a no-op.

    Tests monkeypatch this function to inject synthetic overlays without
    touching the filesystem.
    """
    path = _profile_config_path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        # PyYAML is part of hermes-agent's runtime deps; if it's missing, we
        # gracefully fall back to no overlay rather than crash the plugin.
        import yaml  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: yaml import unavailable, no overlay: %s", exc)
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: failed to parse %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    section = data.get("matrix_coder")
    if not isinstance(section, dict):
        return {}
    return section


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new dict with ``overlay`` keys merged into ``base`` recursively.

    ``overlay`` values win on conflict.  Only dicts are merged recursively;
    everything else (lists, scalars) is replaced wholesale.  This matches the
    semantics of the matrix_coder config — no list overlays are defined.
    """
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config() -> Dict[str, Any]:
    """Return the effective Matrix Coder config: defaults + profile overlay.

    Resolution order (later wins on conflict):
      1. Hardcoded defaults (``_defaults()``).
      2. ``matrix_coder:`` section from the active profile's ``config.yaml``.
      3. The ``MATRIX_CODER_IMPLICIT_ROUTING`` env var, which always wins
         for that one flag (preserved behaviour from the Phase 0 stub).

    Defensive: every failure mode returns at least the defaults.  The plugin
    must never break the hot path because of a config problem.
    """
    config = _defaults()

    overlay = _read_profile_overlay()
    if overlay:
        config = _deep_merge(config, overlay)

    # Env-var override preserved from Phase 0: only the implicit-routing flag
    # is exposed via env.  Other flags are profile-config only.
    config["implicit_routing_enabled"] = (
        os.environ.get("MATRIX_CODER_IMPLICIT_ROUTING", "1") != "0"
    )

    return config
