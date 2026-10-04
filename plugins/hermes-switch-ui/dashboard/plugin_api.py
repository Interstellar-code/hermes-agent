"""FastAPI router for hermes-switch-ui.
Mounted at /api/plugins/hermes-switch-ui/ by web_server._mount_plugin_api_routes().
Mounting is driven by dashboard/manifest.json's "api": "plugin_api.py" entry
(read by web_server._discover_dashboard_plugins()) — NOT by this file's presence.
Loaded flat via spec_from_file_location — NO relative imports. sys.path injection below.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent   # plugins/hermes-switch-ui/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
import _state
import _knowledge
import _version_compat

log = logging.getLogger(__name__)
_PLUGIN_NAME = "hermes-switch-ui"
_VERSION = "0.2.0"


def _require_auth(request: Request) -> None:
    """Raise 401 if request is not authenticated.

    Reuses hermes_cli.web_server._is_authenticated (session cookie / token).
    No-ops gracefully when web_server is not importable (test / standalone context).
    Mirrors standard plugin auth pattern.
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


@router.get("/project-map", dependencies=[Depends(_require_auth)])
def project_map(request: Request, profile: str | None = None):
    """Return all projects + session->project bindings for one profile in one call.

    Read-only: a missing projects.db yields an empty map (never created on read).
    Response: { version, projects: [{id,slug,name,icon,color,archived,board_slug}],
                sessions: {session_id: project_id} }; ETag + 304 on If-None-Match.
    """
    try:
        from hermes_cli import projects_db
        from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists
    except ImportError:
        raise HTTPException(status_code=503, detail="projects backend unavailable")

    name = (profile or "").strip()
    if not name or name == "current":
        db_path = projects_db.projects_db_path()
    else:
        try:
            name = normalize_profile_name(name)
            db_path = get_profile_dir(name) / "projects.db"  # ValueError on unsafe names
            if not profile_exists(name):
                raise HTTPException(status_code=404, detail=f"profile not found: {name}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    projects: list = []
    sessions: dict = {}
    if db_path.is_file():
        with projects_db.connect_closing(db_path=db_path) as conn:
            for r in conn.execute(
                "SELECT id, slug, name, icon, color, archived, board_slug FROM projects "
                "ORDER BY archived, name COLLATE NOCASE"
            ):
                projects.append({
                    "id": r["id"], "slug": r["slug"], "name": r["name"], "icon": r["icon"],
                    "color": r["color"], "archived": bool(r["archived"]), "board_slug": r["board_slug"],
                })
            sessions = {
                r["session_id"]: r["project_id"]
                for r in conn.execute("SELECT session_id, project_id FROM project_sessions")
            }

    payload = {"version": _VERSION, "projects": projects, "sessions": sessions}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    etag = '"' + hashlib.sha1(body.encode()).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    inm = request.headers.get("if-none-match", "")
    if inm.strip() == "*" or etag in (t.strip().removeprefix("W/") for t in inm.split(",")):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)
