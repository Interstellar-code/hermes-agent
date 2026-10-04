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
import sqlite3
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent   # plugins/hermes-switch-ui/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
import _state
import _knowledge
import _version_compat

log = logging.getLogger(__name__)
_PLUGIN_NAME = "hermes-switch-ui"
_VERSION = _version_compat.PLUGIN_VERSION


def _require_auth(request: Request) -> None:
    """Defense-in-depth behind the global dashboard auth middleware.

    Delegates to web_server._require_token; fails CLOSED if it cannot be imported.
    Honors the token-auth seam (request.state.token_authenticated).
    """
    if getattr(request.state, "token_authenticated", False):
        return
    try:
        from hermes_cli.web_server import _require_token
    except ImportError:
        raise HTTPException(status_code=503, detail="auth backend unavailable")
    _require_token(request)


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
def connection_info():
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
    await run_in_threadpool(_state.save_manifest, manifest)
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
    await run_in_threadpool(_state.save_settings, settings)
    return JSONResponse({"ok": True})


@router.get("/status", dependencies=[Depends(_require_auth)])
def status():
    """Return TTL-derived running status.

    frontend polls direction.
    Response: { running, last_heartbeat, ttl_seconds, manifest, reported_settings }
    """
    return _state.get_status()


@router.post("/heartbeat", dependencies=[Depends(_require_auth)])
def heartbeat():
    """Accept an explicit heartbeat ping from SwitchUI.

    Stamps last_heartbeat = now so TTL-based running stays true.
    Response: { ok: true }
    """
    _state.touch_heartbeat()
    return JSONResponse({"ok": True})


_CHAIN_DEPTH_CAP = 50  # parent_session_id walks; also the cycle guard


def _project_rollup(state_db: Path, projects_db: Path) -> dict:
    """Inherited folders + listable counts for one profile, read-only, missing files -> zeros.

    - ``inherited``: {session_id: project_id} for non-subagent sessions with no explicit binding
      whose nearest parent_session_id ancestor has one (explicit always wins; depth-capped walk,
      so cycles terminate).
    - ``counts`` / ``listable_total``: the dashboard list/count rule (``_session_filter_where``,
      exclude_children, archived=exclude -> matches ``profile_totals``). A listed row shows its
      compression TIP, so each listable root is filed under its tip's effective project.
    """
    empty = {"inherited": {}, "counts": {}, "listable_total": 0}
    if not state_db.is_file():
        return empty
    try:
        from hermes_state_common import _ephemeral_child_sql, _sql_json_extract
        from hermes_state_sessions import _session_filter_where, _where_sql
    except ImportError:
        return empty
    where, params = _session_filter_where(exclude_children=True)
    delegate = _sql_json_extract("{a}.model_config", "$._delegate_from")
    conn = sqlite3.connect(f"{state_db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        if not projects_db.is_file():
            total = conn.execute(f"SELECT COUNT(*) FROM sessions s{_where_sql(where, ' ')}", params).fetchone()[0]
            return {**empty, "listable_total": total}
        conn.execute("ATTACH DATABASE ? AS pj", (f"{projects_db.resolve().as_uri()}?mode=ro",))
        # Effective project per non-subagent session: walk up until the first bound node.
        effective: dict = {}
        inherited: dict = {}
        for sid, pid, depth in conn.execute(
            f"""WITH RECURSIVE up(start, cur, depth, pid) AS (
                SELECT s.id, s.id, 0, b.project_id FROM sessions s
                LEFT JOIN pj.project_sessions b ON b.session_id = s.id
                WHERE NOT {_ephemeral_child_sql('s')} AND {delegate.format(a='s')} IS NULL
                UNION ALL
                SELECT up.start, p.parent_session_id, up.depth + 1, b.project_id FROM up
                JOIN sessions p ON p.id = up.cur
                LEFT JOIN pj.project_sessions b ON b.session_id = p.parent_session_id
                WHERE up.pid IS NULL AND p.parent_session_id IS NOT NULL AND up.depth < ?
            ) SELECT start, pid, depth FROM up WHERE pid IS NOT NULL""",
            (_CHAIN_DEPTH_CAP,),
        ):
            effective[sid] = pid
            if depth:
                inherited[sid] = pid
        # Listable roots -> deepest compression descendant (the row the list shows).
        counts: dict = {}
        total = 0
        for _root, tip in conn.execute(
            f"""WITH RECURSIVE down(root, cur, depth) AS (
                SELECT s.id, s.id, 0 FROM sessions s{_where_sql(where, ' ')}
                UNION ALL
                SELECT down.root, c.id, down.depth + 1 FROM down
                JOIN sessions p ON p.id = down.cur AND p.end_reason = 'compression'
                JOIN sessions c ON c.parent_session_id = p.id
                    AND {_sql_json_extract('c.model_config', '$._branched_from')} IS NULL
                    AND {delegate.format(a='c')} IS NULL AND COALESCE(c.source, '') != 'tool'
                WHERE down.depth < ?
            ), ranked AS (
                SELECT root, cur, ROW_NUMBER() OVER (PARTITION BY root ORDER BY depth DESC, cur DESC) AS rn
                FROM down
            ) SELECT root, cur FROM ranked WHERE rn = 1""",
            [*params, _CHAIN_DEPTH_CAP],
        ):
            total += 1
            pid = effective.get(tip)
            if pid:
                counts[pid] = counts.get(pid, 0) + 1
        return {"inherited": inherited, "counts": counts, "listable_total": total}
    except sqlite3.Error as exc:
        log.warning("project-map rollup failed: %s", exc)
        return empty
    finally:
        conn.close()


@router.get("/project-map", dependencies=[Depends(_require_auth)])
def project_map(request: Request, profile: str | None = None):
    """Return all projects + session->project bindings for one profile in one call.

    Read-only: a missing projects.db yields an empty map (never created on read).
    Response: { version, projects: [{id,slug,name,icon,color,archived,board_slug}],
                sessions: {session_id: project_id} (explicit + inherited from the parent chain),
                inherited: {session_id: true}, counts: {project_id: listable sessions},
                listable_total, unfiled }; ETag + 304 on If-None-Match.
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

    rollup = _project_rollup(db_path.parent / "state.db", db_path)
    sessions.update(rollup["inherited"])
    counts, listable_total = rollup["counts"], rollup["listable_total"]
    payload = {
        "version": _VERSION, "projects": projects, "sessions": sessions,
        "inherited": {sid: True for sid in rollup["inherited"]}, "counts": counts,
        "listable_total": listable_total, "unfiled": max(0, listable_total - sum(counts.values())),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    etag = '"' + hashlib.sha1(body.encode()).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    inm = request.headers.get("if-none-match", "")
    if inm.strip() == "*" or etag in (t.strip().removeprefix("W/") for t in inm.split(",")):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)
