"""hermes-switch-ui — knowledge layer.

Loads capability.md and merges any live-registered manifest from _state
(imported lazily/defensively — _state.py arrives in Phase 3).

get_info(refresh=False)
    Returns a dict with static capability doc + optionally merged live
    manifest fields.  refresh=True attempts a best-effort HTTP fetch of
    SWITCHUI_DOCS_URL (swallows all network errors, short timeout).

connection_info()
    Returns gateway/dashboard ports plus best-effort nullable runtime
    fields: active_profile, enabled_plugins, auth_mode.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).resolve().parent
_CAPABILITY_PATH = _PLUGIN_DIR / "capability.md"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_capability_text() -> str:
    """Read capability.md from disk."""
    try:
        return _CAPABILITY_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("hermes-switch-ui: could not read capability.md: %s", exc)
        return ""


def _get_live_manifest() -> Optional[Dict[str, Any]]:
    """Best-effort read of the live manifest from _state (Phase 3 module).

    Returns None if _state is not yet available or raises any error.
    """
    try:
        import _state  # noqa: PLC0415 — lazy/defensive import
        status = _state.get_status()
        manifest = status.get("manifest")
        return manifest if isinstance(manifest, dict) else None
    except ImportError:
        return None
    except Exception as exc:  # noqa: BLE001 — best-effort, never crash
        log.debug("hermes-switch-ui: _get_live_manifest error: %s", exc)
        return None


def _fetch_remote_docs(url: str) -> Optional[str]:
    """Best-effort HTTP fetch of remote capability docs.

    All errors are swallowed.  Returns None on any failure.
    """
    try:
        import urllib.request  # stdlib only — no requests dependency
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-switch-ui/0.1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read(65536)  # cap at 64 KB
            return raw.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        log.debug("hermes-switch-ui: remote docs fetch failed (%s): %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_info(refresh: bool = False) -> Dict[str, Any]:
    """Return capability information about SwitchUI.

    Always includes the static capability.md content.
    Merges live manifest fields if _state is available (Phase 3+).
    refresh=True optionally fetches SWITCHUI_DOCS_URL (best-effort, swallows errors).
    """
    info: Dict[str, Any] = {
        "source": "capability.md",
        "capability": _load_capability_text(),
        "gateway_port": 8642,
        "dashboard_port": 9119,
        "frontend_port": 3002,
        "repo": "https://github.com/Interstellar-code/hermes-switchui",
    }

    # Merge live manifest if available (Phase 3+)
    live = _get_live_manifest()
    if live:
        info["live_manifest"] = live
        # Surface commonly-useful top-level fields
        for key in ("url", "version", "features", "last_registered"):
            if key in live:
                info[key] = live[key]

    # Optional remote refresh
    if refresh:
        docs_url = os.environ.get("SWITCHUI_DOCS_URL", "").strip()
        if docs_url:
            remote = _fetch_remote_docs(docs_url)
            if remote:
                info["remote_docs"] = remote

    return info


def connection_info() -> Dict[str, Any]:
    """Return connection parameters for SwitchUI.

    Hard facts (ports) are always present.
    Runtime fields (active_profile, enabled_plugins, auth_mode) are
    BEST-EFFORT / NULLABLE — None on any failure.
    """
    result: Dict[str, Any] = {
        "gateway_port": 8642,
        "dashboard_port": 9119,
        "frontend_port": 3002,
        "active_profile": None,
        "active_profile_source": None,
        "active_profile_stale": None,
        "enabled_plugins": None,
        "auth_mode": None,
    }

    # Best-effort: read active_profile + enabled_plugins from runtime config
    try:
        from hermes_cli.config import cfg_get, load_config  # noqa: PLC0415
        config = load_config()

        active_profile = cfg_get(config, "profile", default=None)
        result["active_profile"] = active_profile

        plugins_cfg = config.get("plugins")
        if isinstance(plugins_cfg, dict):
            enabled = plugins_cfg.get("enabled")
            if isinstance(enabled, list):
                result["enabled_plugins"] = enabled

        auth_mode = cfg_get(config, "auth", "mode", default=None)
        result["auth_mode"] = auth_mode
    except Exception as exc:  # noqa: BLE001
        log.debug("hermes-switch-ui: connection_info config read failed: %s", exc)

    if result["active_profile"]:
        result["active_profile_source"] = "config"

    # Runtime override (#336): a gateway started with --profile <name> runs with
    # HERMES_HOME=~/.hermes/profiles/<name>. That is what this process is
    # ACTUALLY serving — it beats both the config key and the disk file, which
    # only record intent and can diverge from the running gateway (disk said
    # "morpheus" while this gateway served hermes-switch sessions).
    hermes_home = Path(
        os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    )
    if hermes_home.parent.name == "profiles":
        result["active_profile"] = hermes_home.name
        result["active_profile_source"] = "runtime"
        hermes_root = hermes_home.parent.parent
    else:
        hermes_root = hermes_home

    # Disk fallback (#336): ~/.hermes/active_profile (root, NOT the profile
    # dir) is written by `hermes profile use` / setActiveProfile. Last resort —
    # it names the *selected* profile, which the running gateway may not be on.
    if not result["active_profile"]:
        try:
            name = (hermes_root / "active_profile").read_text(
                encoding="utf-8"
            ).strip()
            if name:
                result["active_profile"] = name
                result["active_profile_source"] = "disk"
        except OSError:
            pass

    # Staleness flag: disk selection differs from what this gateway is running.
    try:
        disk_name = (hermes_root / "active_profile").read_text(
            encoding="utf-8"
        ).strip()
        result["active_profile_stale"] = bool(
            disk_name
            and result["active_profile"]
            and disk_name != result["active_profile"]
        )
    except OSError:
        result["active_profile_stale"] = None

    return result
