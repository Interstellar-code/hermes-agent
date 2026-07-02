"""FastAPI router for the personas plugin.
Mounted at /api/plugins/personas/ by web_server._mount_plugin_api_routes().
Mounting is driven by dashboard/manifest.json's "api": "plugin_api.py" entry
(read by web_server._discover_dashboard_plugins()) — NOT by this file's presence.
Loaded flat via spec_from_file_location — NO relative imports. sys.path injection below.

Thin READ API over the canonical persona store (_library.py). The SwitchUI profile
wizard consumes /list + /get instead of shipping its own copy of the templates.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent   # plugins/personas/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
import _library

log = logging.getLogger(__name__)
_PLUGIN_NAME = "personas"
_VERSION = "0.1.0"

# Local body cap (personas has no _state module to borrow MAX_BODY_BYTES from).
MAX_BODY_BYTES = 32 * 1024


def _require_auth(request: Request) -> None:
    """Raise 401 if request is not authenticated.

    Reuses hermes_cli.web_server._is_authenticated (session cookie / token).
    No-ops gracefully when web_server is not importable (test / standalone context).
    """
    try:
        from hermes_cli.web_server import _is_authenticated  # type: ignore[import]
    except (ImportError, AttributeError):
        return  # web_server not importable (test/standalone) — auth no-ops
    # Import guard is separate from the auth call: an AttributeError (or any
    # error) raised *inside* _is_authenticated must NOT be swallowed into an
    # open-auth bypass — let it surface as a 500.
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


router = APIRouter()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/list", dependencies=[Depends(_require_auth)])
async def list_personas(category: str | None = None):
    """List persona metadata (no full prompt). Optional ?category= filter.

    Response: { personas: [...], count }
    """
    cat = category.strip() if isinstance(category, str) and category.strip() else None
    personas = _library.list_personas(category=cat)
    return {"personas": personas, "count": len(personas)}


@router.get("/get", dependencies=[Depends(_require_auth)])
async def get_persona(id: str):
    """Return the full persona (incl. system_prompt) by id.

    Response: { persona: {...} }  |  404 if unknown.
    """
    persona = _library.get_persona(id)
    if persona is None:
        raise HTTPException(status_code=404, detail=f"persona '{id}' not found")
    return {"persona": persona}


@router.post("/promote", dependencies=[Depends(_require_auth)])
async def promote(request: Request):
    """Promote a T3 persona to a dedicated profile (reserved — not yet implemented).

    The promotion write path (stamping agent.persona_ref into a profile's
    config.yaml + memory inheritance) is a deferred follow-up. The endpoint is
    reserved here so the SwitchUI contract is stable. Returns 501.
    """
    raise HTTPException(
        status_code=501,
        detail="promote is not implemented yet (deferred follow-up; see issue #143)",
    )
