"""
Workflow Engine plugin — FastAPI router.

Phase 3: real handlers delegating to WorkflowEngine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
from typing import Any, AsyncIterator, Dict, List, Optional, get_args

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine bootstrap
# ---------------------------------------------------------------------------
# web_server.py loads this file via spec_from_file_location as a flat module
# (no parent package), so relative imports fail.  We set up sys.path inline
# and use absolute imports so this file works both as a package member and as
# a standalone spec-loaded module.
import sys as _sys
from pathlib import Path
_PLUGIN_DIR = Path(__file__).resolve().parent.parent  # plugins/workflow-engine/
if str(_PLUGIN_DIR) not in _sys.path:
    _sys.path.insert(0, str(_PLUGIN_DIR))
del _sys

from _shared import get_engine  # noqa: E402
from engine import WorkflowEngine  # noqa: E402
from engine.schemas.workflow_run import WorkflowRunStatus  # noqa: E402
from engine.store.definition_store import ConflictError  # noqa: E402

# Engine is initialized lazily on first request via get_engine(); do not call
# it at module load time so that importing this file (e.g. during plugin
# discovery) does not trigger SQLite migrations, seed I/O, or manifest writes.
def _engine() -> WorkflowEngine:  # type: ignore[return]  # noqa: N802
    return get_engine()

router = APIRouter()

_VERSION = "0.4.0"

# Capability flags for clients (SwitchUI feature-detects on these, never on
# _VERSION). Append-only; served by /health as ``features``.
FEATURES: List[str] = [
    "definition_pin", "parent_run",
    "node_attempts", "approver", "node_retrying_event",
    "node_log", "events_query", "sse_db_tail", "cross_process_sse",
    "cron_schedule", "schedules_api",
    "retry_run",
]

_SSE_TAIL_S = 0.5  # run-scoped SSE: DB tail poll interval
_EVENTS_MAX_ROWS = 1000  # GET /runs/{id}/events row cap
_NODE_LOG_TEXT_MAX = 8 * 1024  # per-row node_log text cap on the query (bytes)
_EVENT_TYPES_MAX = 32  # names in GET /runs/{id}/events ?type=
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Validation patterns (mirror TS)
_ID_RE = re.compile(r"^[A-Za-z0-9_:.\-]{1,128}$")
_MAX_YAML_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json(body: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(content=body, status_code=status)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/health")
async def health() -> dict:
    from engine.runtime.scheduler_tick import (  # noqa: PLC0415
        heartbeat_path, profile_for_dir, read_heartbeat,
    )

    db_path = _engine().db_path
    if db_path:
        home = Path(db_path).parent
        alive, at = read_heartbeat(heartbeat_path(home))
        profile = profile_for_dir(home)
    else:  # :memory: engine has no daemon
        alive, at, profile = False, None, "default"
    from engine.nodes.agent_session import routing_config  # noqa: PLC0415
    routing = routing_config(Path(db_path).parent if db_path else None)
    return {
        "ok": True,
        "version": _VERSION,
        "profile": profile,
        "scheduler_alive": alive,
        "scheduler_heartbeat_at": at,
        "routing": {"enabled": routing["enabled"], "allowed_profiles": routing["allowed_profiles"]},
        "features": FEATURES,
    }


# ---------------------------------------------------------------------------
# Definitions — GET /definitions
# ---------------------------------------------------------------------------


@router.get("/definitions")
async def list_definitions(source: Optional[str] = None) -> JSONResponse:
    if source not in (None, "user", "bundled", "project", "all", "system"):
        return _json({"error": "source must be one of user|bundled|project|system|all"}, 400)

    normalized_source = source
    if normalized_source == "system":
        normalized_source = "bundled"
    if normalized_source == "all":
        normalized_source = None

    defs = await _engine().list_definitions(source=normalized_source)
    return _json({"definitions": defs})


# ---------------------------------------------------------------------------
# Definitions — POST /definitions
# ---------------------------------------------------------------------------


@router.post("/definitions")
async def create_definition(request: Request) -> JSONResponse:
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        return _json({"error": "Invalid JSON body"}, 400)

    # Validate id
    if not isinstance(body.get("id"), str) or not _ID_RE.match(body["id"]):
        return _json({"error": "id must be 1-128 chars of [A-Za-z0-9_:.-]"}, 400)
    # Validate name
    name = body.get("name")
    if not isinstance(name, str) or len(name) < 1 or len(name) > 256:
        return _json({"error": "name must be a string 1-256 chars"}, 400)
    # Validate yaml
    yaml_text = body.get("yaml")
    if not isinstance(yaml_text, str) or len(yaml_text) == 0:
        return _json({"error": "yaml must be a non-empty string"}, 400)
    if len(yaml_text.encode("utf-8")) > _MAX_YAML_BYTES:
        return _json({"error": f"yaml exceeds {_MAX_YAML_BYTES} bytes"}, 413)
    # Validate source
    source = body.get("source", "project")
    if source not in ("project", "user", "bundled"):
        return _json({"error": "source must be 'project' | 'user' | 'bundled'"}, 400)
    # Validate scope_path
    scope_path = body.get("scope_path")
    if scope_path is not None:
        if not isinstance(scope_path, str) or not scope_path.startswith("/") or ".." in scope_path:
            return _json({"error": "scope_path must be absolute and contain no .. segments"}, 400)
    # Validate optional fields
    if "description" in body and not isinstance(body["description"], str):
        return _json({"error": "description must be a string when provided"}, 400)
    if "version" in body and not isinstance(body["version"], str):
        return _json({"error": "version must be a string when provided"}, 400)
    tags = body.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            return _json({"error": "tags must be a string[] when provided"}, 400)
    # Optional optimistic-concurrency checksum (CR-1)
    expected_checksum: Optional[str] = body.get("expected_checksum") or None

    # Check if target row is an existing bundled row — route to mark_user_edit
    existing = await _engine().get_definition(body["id"])
    if existing is not None and existing.get("source") == "bundled":
        # Edit-in-place: keep source='bundled', set user_modified=1
        try:
            defn = await _engine().mark_user_edit(
                body["id"],
                yaml_text,
                expected_checksum=expected_checksum,
            )
        except ConflictError as exc:
            return _json({"error": str(exc)}, 409)
        except ValueError as exc:
            return _json({"error": str(exc)}, 422)
        return _json({"definition": defn}, 200)

    # New or non-bundled row: reject explicit source='bundled' creates
    if source == "bundled":
        return _json({"error": "source='bundled' is read-only"}, 403)

    try:
        defn = await _engine().upsert_definition(
            definition_id=body["id"],
            yaml_text=yaml_text,
            source=source,
            source_path=scope_path,
            expected_checksum=expected_checksum,
        )
    except ConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except ValueError as exc:
        return _json({"error": str(exc)}, 422)

    return _json({"definition": defn}, 201)


# ---------------------------------------------------------------------------
# Definitions — GET /definitions/{id}
# ---------------------------------------------------------------------------


@router.get("/definitions/{def_id}")
async def get_definition(def_id: str) -> JSONResponse:
    defn = await _engine().get_definition(def_id)
    if defn is None:
        return _json({"error": "not found"}, 404)
    return _json({"definition": defn})


# ---------------------------------------------------------------------------
# Definitions — GET /definitions/{id}/parsed
# ---------------------------------------------------------------------------


@router.get("/definitions/{def_id}/parsed")
async def get_definition_parsed(def_id: str) -> JSONResponse:
    result = await _engine().parse_definition(def_id)
    if result is None:
        return _json({"error": "not found"}, 404)
    if "error" in result:
        return _json({"error": result["error"]}, 422)
    return _json({"parsed": result})


# ---------------------------------------------------------------------------
# Runs — GET /runs
# ---------------------------------------------------------------------------


@router.get("/runs")
async def list_runs(request: Request) -> JSONResponse:
    params = request.query_params
    workflow_id: Optional[str] = params.get("workflow_id") or None
    status_csv: Optional[str] = params.get("status") or None
    statuses: Optional[List[str]] = None
    if status_csv:
        known = get_args(WorkflowRunStatus)
        statuses = sorted({s.strip() for s in status_csv.split(",")} & set(known))
        if not statuses:
            return _json({"error": f"status must be one of {'|'.join(known)}"}, 400)

    try:
        limit = int(params.get("limit", 50))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 500))

    rows = await _engine().list_runs(
        workflow_id=workflow_id, statuses=statuses, limit=limit,
        parent_run_id=params.get("parent_run_id") or None,
    )
    return _json({"runs": rows})


# ---------------------------------------------------------------------------
# Runs — GET /runs/active  (?scope_path=...)   MUST be before /runs/{run_id}
# ---------------------------------------------------------------------------


@router.get("/runs/active")
async def get_active_run(request: Request) -> JSONResponse:
    scope_path = request.query_params.get("scope_path") or ""
    run = await _engine().get_active_run_by_path(scope_path)
    return _json({"run": run})


# ---------------------------------------------------------------------------
# Runs — GET /runs/by-conversation/{conv_id}   MUST be before /runs/{run_id}
# ---------------------------------------------------------------------------


@router.get("/runs/by-conversation/{conv_id}")
async def find_run_by_conversation(conv_id: str) -> JSONResponse:
    run = await _engine().find_run_by_conversation_id(conv_id)
    if run is None:
        return _json({"run": None})
    return _json({"run": run})


# ---------------------------------------------------------------------------
# Runs — POST /runs
# ---------------------------------------------------------------------------


@router.post("/runs")
async def create_run(request: Request) -> JSONResponse:
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        return _json({"error": "Invalid JSON body"}, 400)

    # Required fields
    if not body.get("workflow_id") or not body.get("conversation_id") or not body.get("user_message"):
        return _json({"error": "workflow_id, conversation_id, user_message required"}, 400)

    workflow_id = body["workflow_id"]
    conversation_id = body["conversation_id"]
    user_message = body["user_message"]

    if not isinstance(workflow_id, str) or not _ID_RE.match(workflow_id):
        return _json({"error": "workflow_id must be 1-128 chars of [A-Za-z0-9_:.-]"}, 400)
    if not isinstance(conversation_id, str) or len(conversation_id) < 1 or len(conversation_id) > 256:
        return _json({"error": "conversation_id must be 1-256 chars"}, 400)
    if not isinstance(user_message, str) or len(user_message) == 0:
        return _json({"error": "user_message must be a non-empty string"}, 400)

    working_path = body.get("working_path")
    if working_path is not None:
        if not isinstance(working_path, str) or not working_path.startswith("/") or ".." in working_path:
            return _json({"error": "working_path must be an absolute path with no .. segments"}, 400)

    # Check definition exists
    defn = await _engine().get_definition(workflow_id)
    if defn is None:
        return _json({"error": f"unknown workflow_id '{workflow_id}'"}, 404)

    trigger: Dict[str, Any] = {
        "kind": "manual",
        "conversation_id": conversation_id,
        "working_path": working_path or "/tmp",
        "user_message": user_message,
    }
    if body.get("parent_conversation_id"):
        trigger["parent_conversation_id"] = body["parent_conversation_id"]
    if body.get("codebase_id"):
        trigger["codebase_id"] = body["codebase_id"]
    # Lineage (RUN AGAIN): carried in the trigger so "at" schedules keep it.
    parent_run_id = body.get("parent_run_id")
    if parent_run_id is not None:
        parent = (
            await _engine().get_run(parent_run_id)
            if isinstance(parent_run_id, str) and _ID_RE.match(parent_run_id) else None
        )
        if parent is None:
            return _json({"error": "parent_run_id not found"}, 400)
        if parent["workflow_id"] != workflow_id:
            return _json({"error": "parent_run_id belongs to a different workflow"}, 400)
        trigger["parent_run_id"] = parent_run_id

    inputs: Dict[str, Any] = body.get("variables") or {}

    # Optional new launch fields: schedule, priority, maxRuntimeSeconds.
    schedule = body.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, dict):
            return _json({"error": "schedule must be an object"}, 400)
        sched_type = schedule.get("type")
        if sched_type not in ("now", "at", "cron"):
            return _json({"error": "schedule.type must be 'now' | 'at' | 'cron'"}, 400)
        if sched_type == "at":
            at_val = schedule.get("at")
            if not isinstance(at_val, str) or not at_val:
                return _json({"error": "schedule.at must be an ISO-8601 string"}, 400)

    priority_raw = body.get("priority", 0)
    if not isinstance(priority_raw, int) or isinstance(priority_raw, bool):
        return _json({"error": "priority must be an integer"}, 400)
    if priority_raw < -100 or priority_raw > 100:
        return _json({"error": "priority must be in [-100, 100]"}, 400)
    priority: int = priority_raw

    max_rt_raw = body.get("maxRuntimeSeconds")
    max_runtime_s: Optional[int] = None
    if max_rt_raw is not None:
        if not isinstance(max_rt_raw, int) or isinstance(max_rt_raw, bool):
            return _json({"error": "maxRuntimeSeconds must be an integer"}, 400)
        if max_rt_raw <= 0 or max_rt_raw > 86400:
            return _json({"error": "maxRuntimeSeconds must be in (0, 86400]"}, 400)
        max_runtime_s = max_rt_raw

    try:
        run = await _engine().schedule_run(
            workflow_id, inputs, trigger,
            schedule=schedule,
            priority=priority,
            max_runtime_s=max_runtime_s,
        )
    except NotImplementedError as exc:
        return _json({"error": str(exc) or "cron schedule not yet supported"}, 501)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)

    return _json({"run": run}, 201)


# ---------------------------------------------------------------------------
# Schedules — native cron ("Repeat") + deferred "at" rows
# ---------------------------------------------------------------------------


@router.get("/schedules")
async def list_schedules(workflow_id: Optional[str] = None) -> JSONResponse:
    return _json({"schedules": await _engine().list_schedules(workflow_id or None)})


@router.patch("/schedules/{schedule_id}")
async def patch_schedule(schedule_id: str, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "Invalid JSON body"}, 400)
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        return _json({"error": "enabled (boolean) is required"}, 400)
    try:
        row = await _engine().set_schedule_enabled(schedule_id, body["enabled"])
    except ConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except ImportError:
        return _json({"error": "cron schedules need croniter"}, 501)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    if row is None:
        return _json({"error": "schedule not found"}, 404)
    return _json({"schedule": row})


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str) -> JSONResponse:
    if not await _engine().cancel_schedule(schedule_id):
        return _json({"error": "schedule not found"}, 404)
    return _json({"ok": True, "id": schedule_id, "status": "cancelled"})


# ---------------------------------------------------------------------------
# Runs — GET /runs/{run_id}
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> JSONResponse:
    run = await _engine().get_run(run_id)
    if run is None:
        return _json({"error": "not found"}, 404)

    # Include node_runs and recent events for UI detail view
    node_runs = _engine()._run_store.list_node_runs(run_id)
    events = _engine()._run_store.list_recent_events(run_id, limit=50)

    return _json({
        "run": run,
        "nodeRuns": node_runs,
        "events": events,
    })


# ---------------------------------------------------------------------------
# Runs — GET /runs/{run_id}/definition  (pinned YAML the run executes)
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/definition")
async def get_run_definition(run_id: str) -> JSONResponse:
    result = await _engine().get_run_definition(run_id)
    if result is None:
        return _json({"error": "not found"}, 404)
    if result["definition"] is None:
        return _json({"error": "definition not found"}, 404)
    return _json(result)


# ---------------------------------------------------------------------------
# Approve — POST /runs/{run_id}/approve
# ---------------------------------------------------------------------------


@router.post("/runs/{run_id}/approve")
async def approve_run(run_id: str, request: Request) -> JSONResponse:
    """
    Approve or reject a paused approval node.

    Note: cross-session ownership is NOT enforced here, matching cancel_run
    below — hermes-switchui is a single-user dev tool and no authenticated
    session is threaded into this HTTP layer. Any caller with gateway auth
    may approve/reject any run. The tool-layer path (tools/approve_workflow.py)
    DOES enforce per-session ownership for agent-initiated approvals.
    """
    run = await _engine().get_run(run_id)
    if run is None:
        return _json({"error": "workflow_run not found"}, 404)

    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        return _json({"error": "Invalid JSON body"}, 400)

    node_run_id = body.get("node_run_id")
    decision = body.get("decision")
    response_text = body.get("response", "")

    if not isinstance(node_run_id, str) or not node_run_id:
        return _json({"error": "node_run_id is required"}, 400)
    if decision not in ("approved", "rejected"):
        return _json({"error": "decision must be 'approved' or 'rejected'"}, 400)
    if not isinstance(response_text, str):
        response_text = ""
    # Self-reported label (SwitchUI sends "switchui"), not an authenticated identity.
    approved_by = body.get("approved_by")
    if approved_by is not None:
        approved_by = approved_by.strip() if isinstance(approved_by, str) else None
        if not approved_by or len(approved_by) > 128 or not approved_by.isprintable():
            return _json({
                "error": "approved_by must be a non-empty printable string of at most 128 characters",
            }, 400)

    # Look up node_run by ID to get the DAG node_id
    node_run = _engine()._run_store.get_node_run(node_run_id)
    if node_run is None:
        return _json({"error": "node_run not found"}, 404)
    if node_run.get("workflow_run_id") != run_id:
        return _json({"error": "node_run does not belong to this workflow_run"}, 400)

    # Map TS decision values to Python facade values
    py_decision = "approve" if decision == "approved" else "reject"
    dag_node_id: str = node_run["dag_node_id"]

    try:
        await _engine().approve(
            run_id=run_id,
            node_id=dag_node_id,
            decision=py_decision,  # type: ignore[arg-type]
            comment=response_text or None,
            actor=approved_by or None,
        )
    except ValueError as exc:
        return _json({"error": str(exc)}, 404)

    return _json({"ok": True, "decision": decision, "resumedRunId": run_id})


# ---------------------------------------------------------------------------
# Cancel — POST /runs/{run_id}/cancel
# ---------------------------------------------------------------------------


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> JSONResponse:
    """
    Cancel a non-terminal workflow run.

    Returns 200 on success, 404 if run not found, 409 if already terminal
    (completed/failed/cancelled).

    Note: cross-session ownership is NOT enforced — hermes-switchui is a
    single-user dev tool. Any caller with gateway auth may cancel any run.
    """
    run = await _engine().get_run(run_id)
    if run is None:
        return _json({"error": "workflow_run not found"}, 404)

    if run.get("status") in ("completed", "failed", "cancelled"):
        return _json({"error": f"run already terminal: {run.get('status')}"}, 409)

    try:
        await _engine().cancel_run(run_id)
    except ValueError as exc:
        return _json({"error": str(exc)}, 404)

    return _json({"ok": True})


# ---------------------------------------------------------------------------
# Events — GET /events  (SSE)
# ---------------------------------------------------------------------------


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    params = request.query_params
    run_id: Optional[str] = params.get("runId") or params.get("run_id") or None
    missing_run = False
    if run_id is not None:
        run = await _engine().get_run(run_id)
        if run is None:
            missing_run = True

    async def _generate() -> AsyncIterator[str]:
        if missing_run:
            yield "event: connected\ndata: {}\n\n"
            yield (
                "event: error\n"
                f"data: {json.dumps({'reason': 'run_not_found', 'run_id': run_id})}\n\n"
            )
            return

        HEARTBEAT_INTERVAL = 15.0
        # Run-scoped streams also tail the DB: runs executed by the gateway or
        # daemon emit on *their* bus, so only their persisted rows reach us.
        it = _engine().subscribe_events(
            run_id, tail_interval_s=_SSE_TAIL_S if run_id else None,
        ).__aiter__()
        pending: Optional["asyncio.Future"] = None

        try:
            while True:
                if await request.is_disconnected():
                    break
                if pending is None:
                    pending = asyncio.ensure_future(it.__anext__())
                # asyncio.wait (unlike wait_for) leaves the pending read alive on timeout
                done, _ = await asyncio.wait({pending}, timeout=HEARTBEAT_INTERVAL)
                if not done:
                    yield "event: ping\ndata: {}\n\n"
                    continue
                try:
                    evt = pending.result()
                except StopAsyncIteration:
                    break
                finally:
                    pending = None

                kind = evt.get("event_type", "event")
                try:
                    data = json.dumps(evt)
                except (TypeError, ValueError):
                    data = json.dumps({"raw": str(evt)})
                yield f"event: {kind}\ndata: {data}\n\n"
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("SSE stream error: %s", exc)
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            if pending is not None:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending
            await it.aclose()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Definitions — DELETE /definitions/{id}
# ---------------------------------------------------------------------------


@router.delete("/definitions/{def_id}")
async def delete_definition(def_id: str) -> JSONResponse:
    defn = await _engine().get_definition(def_id)
    if defn is None:
        return _json({"error": "not found"}, 404)
    if defn.get("source") == "bundled":
        return _json({"error": "bundled definitions are read-only"}, 403)
    try:
        rows = await _engine().delete_definition(def_id)
    except ConflictError as exc:
        return _json({"error": str(exc)}, 409)
    if rows == 0:
        return _json({"error": "not found"}, 404)
    return _json({"ok": True})


# ---------------------------------------------------------------------------
# Definitions — POST /definitions/{id}/reset-factory
# ---------------------------------------------------------------------------

_BUNDLED_DEFAULTS_DIR = None  # resolved lazily below


def _get_bundled_defaults_dir():
    """Return the bundled defaults directory (same path used by seed_defaults)."""
    from pathlib import Path as _P
    return _P(__file__).resolve().parent.parent / "defaults"


_factory_cache: Dict[str, Any] = {"key": None, "items": []}


def _find_factory_yaml(def_id: str):
    """Locate the factory YAML file for def_id in the bundled defaults dir.

    Parsed ids are cached keyed on the files' mtimes (was: re-parse every
    file per call).  Returns (path, content) or (None, None).
    """
    from engine.discovery.validator import validate_workflow_yaml as _vwf
    defaults_dir = _get_bundled_defaults_dir()
    if not defaults_dir.exists():
        return None, None
    files = sorted(defaults_dir.glob("*.yaml"))
    key = tuple((f.name, f.stat().st_mtime_ns) for f in files)
    if _factory_cache["key"] != key:
        items = []
        for yaml_file in files:
            try:
                content = yaml_file.read_text(encoding="utf-8")
                workflow, error = _vwf(content, yaml_file.name)
                if error or not workflow:
                    continue
                fid = workflow.id if workflow.id else yaml_file.stem.lower().replace(" ", "-")
                items.append((fid, yaml_file, content))
            except Exception:
                continue
        _factory_cache.update(key=key, items=items)
    for fid, yaml_file, content in _factory_cache["items"]:
        if fid == def_id:
            return yaml_file, content
    return None, None


@router.post("/definitions/{def_id}/reset-factory")
async def reset_definition_to_factory(def_id: str) -> JSONResponse:
    defn = await _engine().get_definition(def_id)
    if defn is None:
        return _json({"error": "not found"}, 404)
    if defn.get("source") != "bundled":
        return _json({"error": "only bundled definitions can be reset to factory"}, 403)

    _yaml_file, factory_yaml = _find_factory_yaml(def_id)
    if factory_yaml is None:
        return _json({"error": f"no factory file found for id {def_id!r}"}, 404)

    try:
        updated = await _engine().reset_to_factory(def_id, factory_yaml)
    except ValueError as exc:
        return _json({"error": str(exc)}, 422)

    return _json({"definition": updated})


# ---------------------------------------------------------------------------
# Runs — POST /runs/{run_id}/resume
# ---------------------------------------------------------------------------


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str) -> JSONResponse:
    run = await _engine().get_run(run_id)
    if run is None:
        return _json({"error": "not found"}, 404)
    if run.get("status") != "paused":
        return _json({"error": f"run is {run.get('status')}, not paused"}, 409)
    try:
        updated = await _engine().resume_run(run_id)
    except ValueError as exc:
        return _json({"error": str(exc)}, 409)
    return _json({"run": updated})


# ---------------------------------------------------------------------------
# Runs — POST /runs/{run_id}/retry  (B4: re-run a failed / crashed run)
# ---------------------------------------------------------------------------


@router.post("/runs/{run_id}/retry")
async def retry_run(run_id: str, request: Request) -> JSONResponse:
    """Body ``{from_node_id?, actor?}``. 200 ``{run}``; 404 unknown run; 400
    bad body / node not in the pinned definition; 409 not retryable
    (completed / paused), still owned by a live process (its task has not
    ended and its heartbeat is younger than STALE_MS = 300s — also right
    after a cross-process cancel, until the owner finishes its layer), or
    "run already retried" (lost a concurrent retry). Same no-ownership note
    as cancel_run."""
    if not _ID_RE.match(run_id) or await _engine().get_run(run_id) is None:
        return _json({"error": "workflow_run not found"}, 404)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return _json({"error": "Invalid JSON body"}, 400)
    from_node_id = body.get("from_node_id")
    if from_node_id is not None and (
        not isinstance(from_node_id, str) or not 0 < len(from_node_id) <= 128
    ):
        return _json({"error": "from_node_id must be a string of 1-128 characters"}, 400)
    actor = body.get("actor")
    if actor is not None:
        actor = actor.strip() if isinstance(actor, str) else None
        if not actor or len(actor) > 128 or not actor.isprintable():
            return _json({"error": "actor must be a non-empty printable string of at most 128 characters"}, 400)
    try:
        run = await _engine().retry_run(run_id, from_node_id=from_node_id, actor=actor)
    except LookupError as exc:
        return _json({"error": str(exc)}, 404)
    except ConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    return _json({"run": run})


# ---------------------------------------------------------------------------
# Node runs — GET /runs/{run_id}/nodes
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/nodes")
async def list_node_runs(run_id: str) -> JSONResponse:
    run = await _engine().get_run(run_id)
    if run is None:
        return _json({"error": "not found"}, 404)
    node_runs = await _engine().list_node_runs(run_id)
    return _json({"nodeRuns": node_runs})


# ---------------------------------------------------------------------------
# Linked sessions — GET /runs/{run_id}/sessions
# Read-only view into each profile's state.db: the owning chat session plus,
# per node, the agent session, its sub-agents and async delegations.
# ---------------------------------------------------------------------------

_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SESSION_CAP = 200


def _profile_home(profile: str) -> Optional[Path]:
    if not _PROFILE_RE.match(profile or ""):
        return None
    from hermes_constants import get_default_hermes_root  # noqa: PLC0415
    root = get_default_hermes_root()
    return root if profile == "default" else root / "profiles" / profile


def _open_state_db(home: Optional[Path]) -> Optional[sqlite3.Connection]:
    db = home / "state.db" if home else None
    if db is None or not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _session_info(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    cost = d.get("actual_cost_usd")
    return {
        "id": d.get("id"),
        "title": d.get("title"),
        "model": d.get("model"),
        "source": d.get("source"),
        "input_tokens": d.get("input_tokens") or 0,
        "output_tokens": d.get("output_tokens") or 0,
        "cost": cost if cost is not None else d.get("estimated_cost_usd"),
        "cost_source": (
            "actual" if cost is not None
            else "estimated" if d.get("estimated_cost_usd") is not None else None
        ),
        "started_at": d.get("started_at"),
        "ended_at": d.get("ended_at"),
        "end_reason": d.get("end_reason"),
        "last_activity_description": d.get("last_activity_description"),
    }


class _SessionWalker:
    """Walks one profile's state.db; every visited session lands in ``seen`` (totals + cap)."""

    def __init__(self, conn: sqlite3.Connection, seen: Dict[tuple, Dict[str, Any]], profile: str):
        self.conn, self.seen, self.profile = conn, seen, profile
        self.truncated = False
        self.has_deleg = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='async_delegations'"
        ).fetchone() is not None

    def _visit(self, row: sqlite3.Row) -> Optional[Dict[str, Any]]:
        k = (self.profile, row["id"])
        if k in self.seen:
            return None
        if len(self.seen) >= _SESSION_CAP:
            self.truncated = True
            return None
        info = self.seen[k] = _session_info(row)
        return info

    def session(self, sid: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            return None
        return self.seen.get((self.profile, sid)) or self._visit(row)

    def delegations(self, sid: str) -> List[Dict[str, Any]]:
        if not self.has_deleg:
            return []
        out = []
        for r in self.conn.execute(
            "SELECT * FROM async_delegations WHERE parent_session_id = ?"
            " ORDER BY dispatched_at",
            (sid,),
        ):
            d = dict(r)
            try:
                goal = (json.loads(d.get("task_json") or "{}") or {}).get("goal")
            except (ValueError, TypeError, AttributeError):
                goal = None
            out.append({
                "id": d.get("delegation_id"),
                "state": d.get("state"),
                "goal": goal,
                "dispatched_at": d.get("dispatched_at"),
                "completed_at": d.get("completed_at"),
            })
        return out

    def tree(self, sid: str, end_reason: Optional[str]) -> tuple:
        """(children, delegations) of sid. When sid ended by compression its child
        is its continuation (hermes_state_compression.py), so it is folded in."""
        children: List[Dict[str, Any]] = []
        delegs = self.delegations(sid)
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE parent_session_id = ? ORDER BY started_at", (sid,)
        ).fetchall()
        for row in rows:
            info = self._visit(row)
            if info is None:
                continue
            # a sub-agent is never a continuation, even of a compressed parent
            if end_reason == "compression" and row["source"] != "subagent":
                c, dl = self.tree(row["id"], row["end_reason"])
                children += c
                delegs += dl
            else:
                c, dl = self.tree(row["id"], row["end_reason"])
                children.append({
                    **info,
                    "kind": "subagent" if row["source"] == "subagent" else "child",
                    "children": c,
                    "delegations": dl,
                })
        return children, delegs


def _collect_run_sessions(
    run: Dict[str, Any], node_runs: List[Dict[str, Any]], run_profile: str,
) -> Dict[str, Any]:
    conns: List[sqlite3.Connection] = []
    walkers: Dict[str, Optional[_SessionWalker]] = {}
    seen: Dict[tuple, Dict[str, Any]] = {}

    def walker(profile: str) -> Optional[_SessionWalker]:
        if profile not in walkers:
            walkers[profile] = None
            conn = _open_state_db(_profile_home(profile))
            if conn is not None:
                conns.append(conn)
                try:
                    walkers[profile] = _SessionWalker(conn, seen, profile)
                except sqlite3.Error:
                    pass
        return walkers[profile]

    def safe(fn, default):
        try:
            return fn()
        except sqlite3.Error:
            return default

    try:
        owner = None
        if run.get("owner_session"):
            w = walker(run_profile)
            owner = safe(lambda: w.session(run["owner_session"]), None) if w else None

        nodes = []
        for nr in node_runs:
            meta = nr.get("metadata") if isinstance(nr.get("metadata"), dict) else {}
            sid = nr.get("session_id") or meta.get("session_id")
            if not sid:
                continue
            profile = nr.get("assigned_agent") or meta.get("profile") or run_profile
            w = walker(profile)
            info = safe(lambda: w.session(sid), None) if w else None
            children, delegs = (
                safe(lambda: w.tree(sid, info["end_reason"]), ([], [])) if info else ([], [])
            )
            nodes.append({
                "node_run_id": nr.get("id"),
                "dag_node_id": nr.get("dag_node_id"),
                "profile": profile,
                "session_id": sid,
                "gateway_run_id": nr.get("gateway_run_id") or meta.get("gateway_run_id"),
                "session": info,
                "children": children,
                "delegations": delegs,
            })
    finally:
        for c in conns:
            c.close()

    all_s = list(seen.values())
    costs = [s["cost"] for s in all_s]
    return {
        "owner": owner,
        "nodes": nodes,
        "totals": {
            "sessions": len(all_s),
            "subagents": sum(1 for s in all_s if s["source"] == "subagent"),
            "tokens": sum(s["input_tokens"] + s["output_tokens"] for s in all_s),
            "cost_usd": (
                round(sum(costs), 6) if costs and None not in costs else None
            ),
            "truncated": any(w.truncated for w in walkers.values() if w),
        },
    }


@router.get("/runs/{run_id}/sessions")
async def list_run_sessions(run_id: str) -> JSONResponse:
    run = await _engine().get_run(run_id)
    if run is None:
        return _json({"error": "not found"}, 404)
    node_runs = await _engine().list_node_runs(run_id)
    from engine.runtime.scheduler_tick import profile_for_dir  # noqa: PLC0415
    db_path = _engine().db_path
    run_profile = (
        profile_for_dir(Path(db_path).parent) if db_path and db_path != ":memory:" else "default"
    )
    return _json(await asyncio.to_thread(_collect_run_sessions, run, node_runs, run_profile))


# ---------------------------------------------------------------------------
# Node runs — GET /node-runs/active   MUST be before /node-runs/{node_run_id}
# ---------------------------------------------------------------------------


@router.get("/node-runs/active")
async def list_active_node_runs() -> JSONResponse:
    rows = await _engine().list_active_node_runs()
    out = [
        {
            "runId": r.get("run_id"),
            "nodeRunId": r.get("node_run_id"),
            "nodeId": r.get("dag_node_id"),
            "workflowId": r.get("workflow_id"),
            "status": r.get("status"),
            "startedAt": r.get("started_at"),
            "workerId": r.get("worker_id"),
            "sessionId": r.get("session_id"),
            "gatewayRunId": r.get("gateway_run_id"),
        }
        for r in rows
    ]
    return _json({"nodeRuns": out})


# ---------------------------------------------------------------------------
# Node runs — GET /node-runs/{node_run_id}
# ---------------------------------------------------------------------------


@router.get("/node-runs/{node_run_id}")
async def find_node_run_by_id(node_run_id: str) -> JSONResponse:
    nr = await _engine().find_node_run_by_id(node_run_id)
    if nr is None:
        return _json({"error": "not found"}, 404)
    return _json({"nodeRun": nr})


# ---------------------------------------------------------------------------
# Events — POST /runs/{run_id}/events  (append, non-SSE)
# ---------------------------------------------------------------------------


@router.post("/runs/{run_id}/events")
async def append_event(run_id: str, request: Request) -> JSONResponse:
    run = await _engine().get_run(run_id)
    if run is None:
        return _json({"error": "not found"}, 404)
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        return _json({"error": "Invalid JSON body"}, 400)
    if not isinstance(body.get("event_type"), str) or not body["event_type"]:
        return _json({"error": "event_type is required"}, 400)
    body["workflow_run_id"] = run_id
    nr_id = body.get("node_run_id")
    if nr_id is not None:
        nr = await _engine().find_node_run_by_id(nr_id)
        if nr is None or nr.get("workflow_run_id") != run_id:
            return _json({"error": "node_run_id not found for this run"}, 400)
    try:
        await _engine().append_workflow_event(body)
    except sqlite3.IntegrityError as exc:
        return _json({"error": f"invalid event: {exc}"}, 400)
    return _json({"ok": True})


# ---------------------------------------------------------------------------
# Events — GET /runs/{run_id}/events  (JSON array, non-SSE)
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/events")
async def list_run_events(run_id: str, request: Request) -> JSONResponse:
    """Run events, ascending by ``seq``.

    Query: ``limit`` (1-1000, default 200); ``after`` (seq, exclusive) pages
    forward, else the newest ``limit``; ``node_run_id``; ``type`` (csv).
    node_log rows are only returned when ``type`` lists node_log, and then
    each ``data.text`` is capped at 8KB (UTF-8 bytes). ``type`` takes at most
    32 names. ``cursor`` = highest seq returned (pass as the next ``after``).
    """
    run = await _engine().get_run(run_id)
    if run is None:
        return _json({"error": "not found"}, 404)
    q = request.query_params
    try:
        limit = int(q.get("limit", "200"))
    except ValueError:
        limit = 200
    limit = max(1, min(limit, _EVENTS_MAX_ROWS))
    after: Optional[int] = None
    if q.get("after") not in (None, ""):
        try:
            after = max(0, int(q["after"]))
        except ValueError:
            return _json({"error": "after must be an integer seq"}, 400)
    types = [t.strip() for t in (q.get("type") or "").split(",") if t.strip()] or None
    if types and (len(types) > _EVENT_TYPES_MAX or not all(_EVENT_TYPE_RE.match(t) for t in types)):
        return _json({"error": f"type must be at most {_EVENT_TYPES_MAX} comma-separated event names"}, 400)
    events_list = await _engine().query_workflow_events(
        run_id, limit=limit, after=after,
        node_run_id=q.get("node_run_id") or None, types=types,
    )
    for evt in events_list:
        text = (evt.get("data") or {}).get("text") if evt.get("event_type") == "node_log" else None
        raw = text.encode("utf-8") if isinstance(text, str) else b""
        if len(raw) > _NODE_LOG_TEXT_MAX:  # bytes, not chars
            evt["data"]["text"] = raw[:_NODE_LOG_TEXT_MAX].decode("utf-8", "ignore")
            evt["data"]["text_truncated"] = True
    cursor = max((e["seq"] for e in events_list), default=after)
    return _json({"events": events_list, "cursor": cursor})


# ---------------------------------------------------------------------------
# Phase transitions — POST /runs/{run_id}/phase-transitions
# ---------------------------------------------------------------------------


@router.post("/runs/{run_id}/phase-transitions")
async def record_phase_transition(run_id: str, request: Request) -> JSONResponse:
    run = await _engine().get_run(run_id)
    if run is None:
        return _json({"error": "not found"}, 404)
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        return _json({"error": "Invalid JSON body"}, 400)
    to_phase = body.get("toPhase") or body.get("to_phase")
    decided_by = body.get("decidedBy") or body.get("decided_by")
    if not isinstance(to_phase, str) or not to_phase:
        return _json({"error": "toPhase is required"}, 400)
    if not isinstance(decided_by, str) or not decided_by:
        return _json({"error": "decidedBy is required"}, 400)
    try:
        result = await _engine().record_phase_transition(
            run_id=run_id,
            to_phase=to_phase,
            decided_by=decided_by,
            decision_data=body.get("decisionData") or body.get("decision_data"),
        )
    except ValueError as exc:
        return _json({"error": str(exc)}, 422)
    return _json(result)


# ---------------------------------------------------------------------------
# Phase transitions — GET /runs/{run_id}/phase-transitions
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/phase-transitions")
async def list_phase_transitions(run_id: str) -> JSONResponse:
    run = await _engine().get_run(run_id)
    if run is None:
        return _json({"error": "not found"}, 404)
    transitions = await _engine().list_phase_transitions(run_id)
    return _json({"phaseTransitions": transitions})


# ---------------------------------------------------------------------------
# Approval claim — POST /runs/{run_id}/approval-claim
# ---------------------------------------------------------------------------


@router.post("/runs/{run_id}/approval-claim")
async def try_claim_approval_for_resume(run_id: str) -> JSONResponse:
    # ponytail: the raw claim never resumed the run and skipped ownership
    # checks (stuck runs). Approvals go through the workflow_approve tool.
    return _json({"error": "deprecated: use the workflow_approve tool"}, 410)
