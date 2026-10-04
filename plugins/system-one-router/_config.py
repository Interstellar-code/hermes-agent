"""Config loader for system-one-router.

Really reads the profile config.yaml overlay (plugins.system-one-router.*) with an
env escape hatch. Never raises: any I/O or parse failure returns defaults, because
the hot path must not break when the config layer is broken (matrix_coder KB lesson:
a Phase-0 stub that never reads config is the anti-pattern this replaces).
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "base_url": "https://jevtypesafeai.com/api/v1/decide",
    "model": "jev-latest",
    "max_monthly_usd": 2.0,
}

_ENV_KEYS = {
    "SYSTEM_ONE_ROUTER_ENABLED": ("enabled", lambda v: v.strip().lower() in ("1", "true", "yes")),
    "SYSTEM_ONE_ROUTER_BASE_URL": ("base_url", str),
    "SYSTEM_ONE_ROUTER_MODEL": ("model", str),
    "SYSTEM_ONE_ROUTER_MAX_MONTHLY_USD": ("max_monthly_usd", float),
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_yaml_overlay(hermes_home: Path | None) -> dict:
    """Read plugins.system-one-router from the profile config.yaml; {} on any failure."""
    try:
        import yaml  # type: ignore

        home = hermes_home or _default_home()
        cfg_path = home / "config.yaml"
        if not cfg_path.exists():
            return {}
        raw = yaml.safe_load(cfg_path.read_text()) or {}
        plugins = raw.get("plugins") or {}
        overlay = plugins.get("system-one-router")
        return overlay if isinstance(overlay, dict) else {}
    except Exception:
        return {}


def _default_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def load_config(hermes_home: Path | None = None) -> dict[str, Any]:
    cfg = _deep_merge(DEFAULTS, _read_yaml_overlay(hermes_home))
    for env, (key, coerce) in _ENV_KEYS.items():
        val = os.environ.get(env)
        if val is not None and val.strip() != "":
            try:
                cfg[key] = coerce(val)
            except Exception:
                pass
    return cfg
