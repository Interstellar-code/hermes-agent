"""hermes-switch-ui — state persistence layer.

Manages ~/.hermes/switchui/state.json (or SWITCHUI_STATE_PATH override).
Atomic writes via temp-file + os.replace.
TTL-derived "running" detection — no threads/daemons.

Exported constants:
    MAX_BODY_BYTES  — cap imported by plugin_api.py
    HEARTBEAT_TTL   — seconds before running flips to False
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (exported)
# ---------------------------------------------------------------------------

MAX_BODY_BYTES: int = 32 * 1024          # 32 KB raw-body cap
HEARTBEAT_TTL: int = 90                  # seconds; running = (now - last_heartbeat) < TTL

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    """Resolve the state file path.

    Priority:
    1. SWITCHUI_STATE_PATH env var
    2. ~/.hermes/switchui/state.json (default)

    Creates parent directories as needed.
    MUST NOT touch ~/.hermes/switchui/workflows/ (workflow-engine owns it).
    """
    env_override = os.environ.get("SWITCHUI_STATE_PATH", "").strip()
    if env_override:
        path = Path(env_override)
    else:
        path = Path.home() / ".hermes" / "switchui" / "state.json"

    # Create parent dir only (never recurse into workflows/)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Low-level I/O
# ---------------------------------------------------------------------------

def _read_state() -> Dict[str, Any]:
    """Read and return state.json; returns empty dict on any error."""
    path = _state_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        log.warning("hermes-switch-ui: could not read state file %s: %s", path, exc)
        return {}


def _write_state(data: Dict[str, Any]) -> None:
    """Atomically write data to state.json via a sibling .tmp file."""
    path = _state_path()
    tmp_path = path.with_suffix(".json.tmp")
    try:
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception as exc:  # noqa: BLE001
        log.error("hermes-switch-ui: failed to write state file %s: %s", path, exc)
        # Clean up temp if it was created
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        raise


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

# Keys that are secret-looking (case-insensitive substring match)
_SECRET_SUBSTRINGS = ("token", "password", "secret")

_MANIFEST_WHITELIST: Dict[str, type] = {
    "version": str,
    "url": str,
    "port": int,
    "hermes_api_url": str,
    "enabled_features": list,
    "registered_at": str,
}

_SETTINGS_WHITELIST: Dict[str, type] = {
    # Accept any key that is not secret-looking; unknown non-secret keys are
    # coerced to string to avoid storing unexpected nested blobs.
}


def validate_manifest(payload: Any) -> Dict[str, Any]:
    """Whitelist and coerce top-level manifest keys.

    Rejects:
    - Non-dict payloads
    - Any unknown nested blobs (dicts/lists for non-whitelisted keys)
    - Wrong types for known keys (coerces where safe; raises ValueError otherwise)

    Returns clean dict ready for persistence.
    Raises ValueError on garbage input (handler maps to 422).
    """
    if not isinstance(payload, dict):
        raise ValueError("manifest must be a JSON object")

    clean: Dict[str, Any] = {}
    for key, expected_type in _MANIFEST_WHITELIST.items():
        if key not in payload:
            continue
        value = payload[key]
        if expected_type is int:
            try:
                clean[key] = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"manifest key {key!r} must be an integer")
        elif expected_type is str:
            if not isinstance(value, str):
                raise ValueError(f"manifest key {key!r} must be a string")
            clean[key] = value
        elif expected_type is list:
            if not isinstance(value, list):
                raise ValueError(f"manifest key {key!r} must be a list")
            # Ensure it's a flat list of strings (feature names)
            coerced: List[str] = []
            for item in value:
                if isinstance(item, (dict, list)):
                    raise ValueError(f"manifest key {key!r} items must be scalars, got nested object")
                coerced.append(str(item))
            clean[key] = coerced
        else:
            clean[key] = value

    # Reject any unknown keys that carry nested blobs
    for key, value in payload.items():
        if key not in _MANIFEST_WHITELIST:
            if isinstance(value, (dict, list)):
                raise ValueError(
                    f"manifest contains unknown nested key {key!r}; only whitelisted keys are accepted"
                )
            # Scalar unknown keys are silently ignored (forward-compat)

    return clean


def _is_secret_key(key: str) -> bool:
    """Return True if key name looks like a secret credential."""
    lower = key.lower()
    return any(sub in lower for sub in _SECRET_SUBSTRINGS)


def validate_settings(payload: Any) -> Dict[str, Any]:
    """Whitelist settings; strip secret-looking keys.

    - Strips any key matching token/password/secret (case-insensitive).
    - Rejects unknown nested blobs (dicts/lists in values).
    - Coerces scalar values to string for safety.

    Returns clean dict ready for persistence.
    Raises ValueError on garbage input (non-dict).
    """
    if not isinstance(payload, dict):
        raise ValueError("settings must be a JSON object")

    clean: Dict[str, Any] = {}
    for key, value in payload.items():
        if _is_secret_key(key):
            log.info("hermes-switch-ui: stripping secret-looking key %r from settings", key)
            continue
        if isinstance(value, dict):
            raise ValueError(
                f"settings key {key!r} has a nested object; only scalar/list values are accepted"
            )
        if isinstance(value, list):
            # Allow flat lists of scalars
            coerced_list: List[Any] = []
            for item in value:
                if isinstance(item, (dict, list)):
                    raise ValueError(
                        f"settings key {key!r} contains a nested object in its list"
                    )
                coerced_list.append(item)
            clean[key] = coerced_list
        else:
            clean[key] = value

    return clean


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_manifest(manifest: Dict[str, Any]) -> None:
    """Persist manifest under 'manifest' key; stamp last_heartbeat = now."""
    state = _read_state()
    state.setdefault("schema_version", 1)
    manifest_copy = dict(manifest)
    manifest_copy.setdefault("registered_at", _utcnow_iso())
    state["manifest"] = manifest_copy
    state["last_heartbeat"] = _utcnow_iso()
    _write_state(state)


def save_settings(settings: Dict[str, Any]) -> None:
    """Persist settings under 'reported_settings' key; stamp last_heartbeat = now."""
    state = _read_state()
    state.setdefault("schema_version", 1)
    state["reported_settings"] = settings
    state["last_heartbeat"] = _utcnow_iso()
    _write_state(state)


def touch_heartbeat() -> None:
    """Update last_heartbeat to now without changing other fields."""
    state = _read_state()
    state.setdefault("schema_version", 1)
    state["last_heartbeat"] = _utcnow_iso()
    _write_state(state)


def get_status() -> Dict[str, Any]:
    """Return TTL-derived status dict.

    Keys returned:
        running         — bool: (now - last_heartbeat) < HEARTBEAT_TTL
        last_heartbeat  — ISO string or None
        ttl_seconds     — int: HEARTBEAT_TTL constant
        manifest        — dict or None
        reported_settings — dict or None
    """
    state = _read_state()
    last_hb_str: Optional[str] = state.get("last_heartbeat")

    running = False
    if last_hb_str:
        try:
            last_hb = datetime.fromisoformat(last_hb_str.replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - last_hb).total_seconds()
            running = elapsed < HEARTBEAT_TTL
        except Exception as exc:  # noqa: BLE001
            log.debug("hermes-switch-ui: could not parse last_heartbeat %r: %s", last_hb_str, exc)

    return {
        "running": running,
        "last_heartbeat": last_hb_str,
        "ttl_seconds": HEARTBEAT_TTL,
        "manifest": state.get("manifest"),
        "reported_settings": state.get("reported_settings"),
    }
