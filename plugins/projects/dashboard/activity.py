"""Bounded mixed task/session activity for one project."""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from pathlib import Path

from hermes_cli import kanban_db, projects_db
from hermes_constants import get_hermes_home

log = logging.getLogger(__name__)


def _cursor(project_id: str, key: tuple[int, str]) -> str:
    raw = json.dumps([1, project_id, key[0], key[1]], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None) -> tuple[str, int, str] | None:
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or len(parsed) != 4 or parsed[0] != 1:
            raise ValueError
        return str(parsed[1]), int(parsed[2]), str(parsed[3])
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid activity cursor") from exc


def _task_activity(project_id: str) -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    errors: list[str] = []
    try:
        boards = kanban_db.list_boards(include_archived=True)
    except Exception as exc:
        return [], [f"boards: {exc}"]

    seen: set[str] = set()
    for board in boards:
        slug = str(board.get("slug") or kanban_db.DEFAULT_BOARD)
        raw_path = board.get("db_path")
        conn: sqlite3.Connection | None = None
        try:
            path = Path(raw_path).expanduser() if raw_path else kanban_db.kanban_db_path(slug)
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            conn = kanban_db.connect(db_path=path)
            rows = conn.execute(
                """
                SELECT t.id, t.title, t.status, t.assignee, t.created_at,
                       COALESCE(MAX(e.created_at), t.created_at) AS occurred_at,
                       (SELECT e2.kind FROM task_events e2
                         WHERE e2.task_id = t.id
                      ORDER BY e2.created_at DESC, e2.id DESC LIMIT 1) AS event_kind
                  FROM tasks t
             LEFT JOIN task_events e ON e.task_id = t.id
                 WHERE t.project_id = ? AND t.status != 'archived'
              GROUP BY t.id
                """,
                (project_id,),
            ).fetchall()
            for row in rows:
                items.append(
                    {
                        "kind": "task",
                        "id": row["id"],
                        "occurred_at": int(row["occurred_at"]),
                        "event_kind": row["event_kind"],
                        "board_slug": slug,
                        "title": row["title"],
                        "status": row["status"],
                        "assignee": row["assignee"],
                        "created_at": int(row["created_at"]),
                    }
                )
        except Exception as exc:
            log.warning("project task activity failed for %s: %s", slug, exc)
            errors.append(f"board {slug}: {exc}")
        finally:
            if conn is not None:
                conn.close()
    return items, errors


def _session_activity(project: projects_db.Project) -> tuple[list[dict], list[str]]:
    path = get_hermes_home() / "state.db"
    if not path.exists():
        return [], []
    try:
        from hermes_state import SessionDB
        from tui_gateway import git_probe, project_tree

        db = SessionDB(db_path=path, read_only=True)
        try:
            index = project_tree._FolderIndex([project.to_dict()])
            items: list[dict] = []
            offset = 0
            while True:
                rows = db.list_sessions_rich(
                    limit=2_000,
                    offset=offset,
                    order_by_last_active=True,
                    min_message_count=1,
                    include_children=False,
                    exclude_sources=["cron"],
                    include_archived=False,
                )
                for row in rows:
                    if project_tree._project_for_session(row, index, git_probe.resolve) is None:
                        continue
                    items.append(
                        {
                            "kind": "session",
                            "id": row["id"],
                            "occurred_at": int(float(row.get("last_active") or row.get("started_at") or 0)),
                            "title": row.get("title"),
                            "preview": row.get("preview") or "",
                            "source": row.get("source"),
                            "model": row.get("model"),
                            "message_count": int(row.get("message_count") or 0),
                            "cwd": row.get("cwd"),
                        }
                    )
                if len(rows) < 2_000:
                    break
                offset += len(rows)
        finally:
            db.close()
        return items, []
    except Exception as exc:
        log.warning("project session activity failed: %s", exc)
        return [], [f"sessions: {exc}"]


def project_activity(
    project: projects_db.Project, *, limit: int, after: tuple[int, str] | None
) -> dict:
    tasks, errors = _task_activity(project.id)
    sessions, session_errors = _session_activity(project)
    errors.extend(session_errors)

    items = tasks + sessions
    for item in items:
        item["_key"] = f'{item["kind"]}:{item.get("board_slug", "")}:{item["id"]}'
    items.sort(key=lambda item: (item["occurred_at"], item["_key"]), reverse=True)
    if after is not None:
        items = [item for item in items if (item["occurred_at"], item["_key"]) < after]

    page = items[: limit + 1]
    has_more = len(page) > limit
    page = page[:limit]
    next_cursor = (
        _cursor(project.id, (page[-1]["occurred_at"], page[-1]["_key"]))
        if has_more and not errors
        else None
    )
    for item in page:
        item.pop("_key", None)
    response = {"project_id": project.id, "items": page, "next_cursor": next_cursor}
    if errors:
        response["activity_errors"] = errors
    return response
