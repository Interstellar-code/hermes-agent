"""Render a bound session's Project as a system-prompt block.

The bridge between the 0.19 project↔session binding model
(``projects_db.bind_session`` / ``get_session_project``) and the agent's
CACHED system prompt.  It is read once, when that prompt is first built, and
never again for the life of the conversation — see
``agent.system_prompt.build_system_prompt_parts``.

Two rules follow from that single read, and both are deliberate:

* **Only stable metadata.** Nothing here may reflect anything that moves
  while a conversation runs — no task counts, no activity timestamps, no
  session counts.  A cached prefix cannot be corrected once it is wrong, so
  a value that goes stale is worse than a value that was never included.
* **Fail closed.** A missing binding, a deleted project, an unreadable
  projects.db — all of them yield an empty block, never an exception.  A
  broken project record must not stop an agent from starting.

Rebinding mid-conversation intentionally has no effect on that conversation:
the next turn restores the stored prompt byte-for-byte.  A new session picks
up the new binding when it builds its own prompt.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# Folder lists are unbounded in the DB and this text is paid for on every
# single API call of the conversation. Cap, and say so when truncating: a
# silently short list reads as a complete one.
MAX_FOLDERS = 10


def _folder_lines(project: Any) -> List[str]:
    """Bounded, deterministically ordered folder lines.

    ``_load_folders`` already orders ``is_primary DESC, added_at ASC``, so the
    order is a property of the record, not of this call.
    """
    paths: List[str] = []
    for folder in getattr(project, "folders", None) or []:
        path = str(getattr(folder, "path", "") or "").strip()
        if path and path not in paths:
            paths.append(path)

    lines = [f"- {path}" for path in paths[:MAX_FOLDERS]]
    hidden = len(paths) - MAX_FOLDERS
    if hidden > 0:
        lines.append(f"- (+{hidden} more not shown)")
    return lines


def format_project_context(project: Any) -> str:
    """Format one project record as the Project Context block."""
    name = str(getattr(project, "name", "") or "").strip()
    slug = str(getattr(project, "slug", "") or "").strip()
    if not name and not slug:
        return ""

    header = name or slug
    if slug and name:
        header = f"{name} (`{slug}`)"

    lines = ["## Project Context", f"Project: {header}"]

    project_id = str(getattr(project, "id", "") or "").strip()
    if project_id:
        lines.append(f"Project ID: {project_id}")

    primary_path = str(getattr(project, "primary_path", "") or "").strip()
    if primary_path:
        lines.append(f"Primary path: {primary_path}")

    folder_lines = _folder_lines(project)
    if folder_lines:
        lines.append("Folders:")
        lines.extend(folder_lines)

    board_slug = str(getattr(project, "board_slug", "") or "").strip()
    if board_slug:
        lines.append(f"Bound board: {board_slug}")

    return "\n".join(lines)


def project_context_block(session_id: Optional[str]) -> str:
    """The Project Context block for ``session_id``, or ``""`` when unbound.

    Resolution is the EXPLICIT binding only. There is deliberately no
    cwd-match or active-project fallback here: those are interactive
    conveniences for picking a project to show a human, and guessing wrong in
    a cached system prompt would tell the model it is working somewhere it is
    not, for the whole conversation.
    """
    if not session_id or not str(session_id).strip():
        return ""
    try:
        from hermes_cli import projects_db

        with projects_db.connect_closing() as conn:
            binding = projects_db.get_session_project(conn, session_id)
            if binding is None:
                return ""
            project = projects_db.get_project(conn, binding.project_id)
        if project is None:
            # Orphaned binding: the project row is gone (deleted between bind
            # and now). Nothing truthful left to say.
            logger.debug(
                "Session %s is bound to missing project %s; no project context.",
                session_id, binding.project_id,
            )
            return ""
        return format_project_context(project)
    except Exception as exc:  # noqa: BLE001 — never block agent startup.
        logger.warning(
            "Could not resolve project context for session %s: %s", session_id, exc
        )
        return ""
