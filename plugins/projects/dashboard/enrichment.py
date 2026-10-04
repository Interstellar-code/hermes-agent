"""Best-effort cross-store fields for the Projects dashboard API."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from hermes_cli import kanban_db
# connect() is NOT re-exported from kanban_db any more: that path is a
# plugin-compat shim whose stated removal date (2026-09-14) has already
# passed. Import it from the module that actually owns it.
from hermes_cli import kanban_db_connect
from hermes_cli import projects_db
from hermes_constants import get_hermes_home

log = logging.getLogger(__name__)

_SESSION_PAGE_SIZE = 2_000


def _empty_task_counts() -> dict[str, int]:
    return {status: 0 for status in sorted(kanban_db.VALID_STATUSES - {"archived"})}


def iter_board_paths(errors: list[str], default_slug: str = ""):
    """Yield (slug, db_path) once per distinct kanban board DB; list failures into ``errors``."""
    try:
        boards = kanban_db.list_boards(include_archived=True)
    except Exception as exc:
        errors.append(f"boards: {exc}")
        return
    seen: set[str] = set()
    for board in boards:
        slug = str(board.get("slug") or default_slug)
        if not slug:
            continue
        try:
            raw_path = board.get("db_path")
            path = Path(raw_path).expanduser() if raw_path else kanban_db.kanban_db_path(slug)
            resolved = str(path.resolve())
        except Exception as exc:
            errors.append(f"board {slug}: {exc}")
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        yield slug, path


def iter_session_pages(db):
    """Yield listable session pages (newest first) until exhausted."""
    offset = 0
    while True:
        rows = db.list_sessions_rich(
            limit=_SESSION_PAGE_SIZE,
            offset=offset,
            order_by_last_active=True,
            min_message_count=1,
            include_children=False,
            exclude_sources=["cron"],
            include_archived=False,
        )
        yield rows
        if len(rows) < _SESSION_PAGE_SIZE:
            return
        offset += len(rows)


def _board_enrichment(project_ids: set[str], boards: list, errors: list[str]) -> tuple[dict[str, dict], list[str], bool]:
    """Aggregate linked tasks/events once per shared board."""
    totals: dict[str, dict] = {}
    for slug, db_path in boards:
        conn: sqlite3.Connection | None = None
        try:
            conn = kanban_db_connect.connect(db_path=db_path)
            rows = conn.execute(
                """
                SELECT t.project_id,
                       COUNT(DISTINCT CASE WHEN t.status != 'archived' THEN t.id END) AS total,
                       COUNT(DISTINCT CASE WHEN t.status NOT IN ('done', 'archived') THEN t.id END) AS open,
                       MAX(e.created_at) AS last_event,
                       t.status
                  FROM tasks t
             LEFT JOIN task_events e ON e.task_id = t.id
                 WHERE t.project_id IS NOT NULL
              GROUP BY t.project_id, t.status
                """
            ).fetchall()
            for row in rows:
                pid = str(row["project_id"])
                if pid not in project_ids:
                    continue
                item = totals.setdefault(
                    pid,
                    {
                        "task_count": 0,
                        "open_task_count": 0,
                        "last_event": None,
                        "task_status_counts": _empty_task_counts(),
                    },
                )
                item["task_count"] += int(row["total"] or 0)
                item["open_task_count"] += int(row["open"] or 0)
                if row["status"] != "archived":
                    item["task_status_counts"].setdefault(row["status"], 0)
                    item["task_status_counts"][row["status"]] += int(row["total"] or 0)
                event_at = row["last_event"]
                if event_at is not None:
                    item["last_event"] = max(item["last_event"] or 0, int(event_at))
        except Exception as exc:
            log.warning("project board enrichment failed for %s: %s", slug, exc)
            errors.append(f"board {slug}: {exc}")
        finally:
            if conn is not None:
                conn.close()
    return totals, errors, not errors


def _session_activity(projects: list[projects_db.Project]) -> tuple[dict[str, dict], list[str]]:
    """Return count/activity for listable sessions using all project folders."""
    db_path = get_hermes_home() / "state.db"
    if not db_path.exists():
        return {}, []

    try:
        from tui_gateway import git_probe, project_tree
        from hermes_state import SessionDB

        db = SessionDB(db_path=db_path, read_only=True)
        projects_conn = None
        try:
            projects_conn = projects_db.connect()
            project_dicts = [p.to_dict() for p in projects if not p.archived]
            active_project_ids = {
                project.id for project in projects_db.list_projects(projects_conn)
            }
            index = project_tree._FolderIndex(project_dicts)
            activity: dict[str, dict] = {}
            for sessions in iter_session_pages(db):
                explicit_owners = projects_db.get_session_projects(
                    projects_conn, (session["id"] for session in sessions)
                )
                for session in sessions:
                    binding = explicit_owners.get(session["id"])
                    owner_id = (
                        binding.project_id
                        if binding and binding.project_id in active_project_ids
                        else None
                    )
                    owner = {"id": owner_id} if owner_id else project_tree._project_for_session(
                        session, index, git_probe.resolve
                    )
                    if owner is None:
                        continue
                    at = int(float(session.get("last_active") or session.get("started_at") or 0))
                    item = activity.setdefault(owner["id"], {"session_count": 0, "last_activity": None})
                    item["session_count"] += 1
                    if at:
                        item["last_activity"] = max(item["last_activity"] or 0, at)
        finally:
            if projects_conn is not None:
                projects_conn.close()
            db.close()
        return activity, []
    except Exception as exc:
        log.warning("project session enrichment failed: %s", exc)
        return {}, [f"sessions: {exc}"]


def _bound_board(slug: str | None) -> dict | None:
    if not slug:
        return None
    try:
        if not kanban_db.board_exists(slug):
            return None
        meta = kanban_db.read_board_metadata(slug)
        return {
            key: meta.get(key)
            for key in ("slug", "name", "description", "icon", "color", "archived")
        }
    except Exception:
        return None


_CACHE_TTL = 5.0
_cache: dict = {}


def _stat_sig(paths) -> tuple:
    sig = []
    for p in paths:
        for suffix in ("", "-wal"):
            try:
                st = Path(str(p) + suffix).stat()
                sig.append((str(p) + suffix, st.st_mtime_ns, st.st_size))
            except OSError:
                pass
    return tuple(sig)


def _cached_aggregates(rows: list[projects_db.Project]):
    """Board + session aggregates, memoised for a few seconds.

    Key = home + project folder layout + mtimes of every DB the aggregates read, so any
    write invalidates it. ponytail: mtime check is O(boards) stats; TTL caps staleness for
    git-probe-derived ownership.
    """
    home = get_hermes_home()
    paths = [home / "state.db", projects_db.projects_db_path()]
    probe: list[str] = []
    boards = list(iter_board_paths(probe))
    paths += [p for _, p in boards]
    key = (
        str(home),
        tuple((p.id, p.archived, p.board_slug, tuple(f.path for f in p.folders)) for p in rows),
        _stat_sig(paths),
    )
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL and not probe:
        return hit[1]
    board = _board_enrichment({p.id for p in rows}, boards, probe)
    sess = _session_activity(rows)
    result = (board, sess)
    _cache.clear()
    _cache[key] = (time.monotonic(), result)
    return result


def enrich_projects(projects: Iterable[projects_db.Project], active_id: str | None) -> tuple[list[dict], list[str]]:
    """Add frontend fields without changing ``Project.to_dict()``."""
    rows = list(projects)
    project_ids = {p.id for p in rows}
    (board_data, errors, boards_available), (session_data, session_errors) = _cached_aggregates(rows)
    errors = list(errors)
    sessions_available = not session_errors
    errors.extend(session_errors)

    enriched: list[dict] = []
    for project in rows:
        data = project.to_dict()
        derived = board_data.get(project.id, {})
        event_at = derived.get("last_event")
        session = session_data.get(project.id, {})
        session_at = session.get("last_activity")
        activity = (
            max((at for at in (event_at, session_at) if at is not None), default=None)
            if boards_available and sessions_available
            else None
        )
        data.update(
            {
                "task_count": int(derived.get("task_count", 0)) if boards_available else None,
                "open_task_count": int(derived.get("open_task_count", 0)) if boards_available else None,
                "task_status_counts": derived.get("task_status_counts", _empty_task_counts()) if boards_available else None,
                "session_count": int(session.get("session_count", 0)) if sessions_available else None,
                "last_task_activity_at": event_at if boards_available else None,
                "last_session_activity_at": session_at if sessions_available else None,
                "last_activity_at": activity,
                "is_active": project.id == active_id,
                "folder_count": len(project.folders),
                "bound_board": _bound_board(project.board_slug),
            }
        )
        enriched.append(data)
    return enriched, errors
