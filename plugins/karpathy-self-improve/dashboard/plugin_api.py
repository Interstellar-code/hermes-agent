"""
FastAPI router for karpathy-self-improve.
Mounted at /api/plugins/karpathy-self-improve/ by web_server._mount_plugin_api_routes().

IMPORTANT: web_server loads this file with spec_from_file_location as a flat
module — NO parent package. Relative imports FAIL. We use sys.path injection
below so absolute imports resolve both here and in tests.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

# Fix imports: add plugin root so _db and _metrics resolve as top-level modules.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent  # plugins/karpathy-self-improve/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from fastapi import APIRouter
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

_VERSION = "0.1.0"
_PLUGIN_NAME = "karpathy-self-improve"

# Module-level router — web_server looks for exactly this name.
router = APIRouter()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health")
async def health() -> dict:
    return {"ok": True, "plugin": _PLUGIN_NAME, "version": _VERSION}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@router.get("/metrics")
async def list_metrics(
    profile: Optional[str] = None,
    limit: int = 100,
) -> JSONResponse:
    try:
        from _db import get_db
        rows = get_db().list_metrics(profile=profile, limit=limit)
        return JSONResponse({"metrics": rows})
    except Exception as exc:
        log.exception("karpathy /metrics error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/metrics/latest")
async def latest_metrics() -> JSONResponse:
    try:
        from _db import get_db
        rows = get_db().latest_metrics_per_profile()
        return JSONResponse({"metrics": rows})
    except Exception as exc:
        log.exception("karpathy /metrics/latest error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/metrics/collect")
async def collect_metrics() -> JSONResponse:
    try:
        from _metrics import collect_profile_metrics
        snapshots = collect_profile_metrics()
        return JSONResponse({"collected": len(snapshots), "snapshots": snapshots})
    except Exception as exc:
        log.exception("karpathy /metrics/collect error")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------

@router.get("/experiments")
async def list_experiments(
    profile: Optional[str] = None,
    state: Optional[str] = None,
) -> JSONResponse:
    try:
        from _db import get_db
        rows = get_db().list_experiments(profile=profile, state=state)
        return JSONResponse({"experiments": rows})
    except Exception as exc:
        log.exception("karpathy /experiments error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/experiments/{exp_id}")
async def get_experiment(exp_id: int) -> JSONResponse:
    try:
        from _db import get_db
        row = get_db().get_experiment(exp_id)
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(row)
    except Exception as exc:
        log.exception("karpathy /experiments/%s error", exp_id)
        return JSONResponse({"error": str(exc)}, status_code=500)
