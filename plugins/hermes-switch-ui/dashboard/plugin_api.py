"""FastAPI router for hermes-switch-ui.
Mounted at /api/plugins/hermes-switch-ui/ by web_server._mount_plugin_api_routes().
Mounting is driven by dashboard/manifest.json's "api": "plugin_api.py" entry
(read by web_server._discover_dashboard_plugins()) — NOT by this file's presence.
Loaded flat via spec_from_file_location — NO relative imports. sys.path injection below.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent   # plugins/hermes-switch-ui/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
import _state
import _knowledge
import _version_compat

log = logging.getLogger(__name__)
_PLUGIN_NAME = "hermes-switch-ui"
_VERSION = "0.1.0"


def _require_auth(request: Request) -> None:
    """Raise 401 if request is not authenticated.

    Reuses hermes_cli.web_server._is_authenticated (session cookie / token).
    No-ops gracefully when web_server is not importable (test / standalone context).
    Mirrors karpathy-self-improve pattern.
    """
    try:
        from hermes_cli.web_server import _is_authenticated  # type: ignore[import]
        if not _is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
    except (ImportError, AttributeError):
        pass  # test/standalone — auth no-ops


router = APIRouter()


async def _read_capped_json(request: Request) -> dict:
    """Read and parse request body, enforcing MAX_BODY_BYTES cap.

    Body cap is applied to raw bytes BEFORE JSON parsing so oversized payloads
    are rejected before any deserialization work (Codex review requirement).
    Returns parsed dict.
    Raises HTTPException 413 on oversized body, 422 on invalid JSON.
    """
    raw = await request.body()
    if len(raw) > _state.MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Body too large")
    try:
        return json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid JSON")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/connection", dependencies=[Depends(_require_auth)])
async def connection_info():
    """Return backend connection parameters for SwitchUI.

    backend -> frontend direction.
    Response: { gateway_port, dashboard_port, frontend_port, active_profile,
                enabled_plugins, auth_mode }
    """
    return _knowledge.connection_info()


@router.post("/register", dependencies=[Depends(_require_auth)])
async def register_frontend(request: Request):
    """Accept a live registration manifest from SwitchUI.

    frontend -> backend direction.
    Validates and persists the manifest, stamps last_heartbeat, checks version compat.
    Response: { ok: true, compat: { compatible, warn, plugin_range, frontend_version } }
    """
    payload = await _read_capped_json(request)
    try:
        manifest = _state.validate_manifest(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    _state.save_manifest(manifest)
    compat = _version_compat.check(manifest.get("version"))
    return JSONResponse({"ok": True, "compat": compat})


@router.post("/settings", dependencies=[Depends(_require_auth)])
async def report_settings(request: Request):
    """Accept a settings report from SwitchUI.

    frontend -> backend direction.
    Strips secret-looking keys, validates, and persists.
    Response: { ok: true }
    """
    payload = await _read_capped_json(request)
    try:
        settings = _state.validate_settings(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    _state.save_settings(settings)
    return JSONResponse({"ok": True})


@router.get("/status", dependencies=[Depends(_require_auth)])
async def status():
    """Return TTL-derived running status.

    frontend polls direction.
    Response: { running, last_heartbeat, ttl_seconds, manifest, reported_settings }
    """
    return _state.get_status()


@router.post("/heartbeat", dependencies=[Depends(_require_auth)])
async def heartbeat():
    """Accept an explicit heartbeat ping from SwitchUI.

    Stamps last_heartbeat = now so TTL-based running stays true.
    Response: { ok: true }
    """
    _state.touch_heartbeat()
    return JSONResponse({"ok": True})
