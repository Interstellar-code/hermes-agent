"""Audit-mirror of Matrix Coder invocations onto the Hermes Kanban board.

Phase 2 OBSERVABILITY layer (NOT control flow). Each ``matrix ...`` invocation
is mirrored as ONE Kanban card so it shows up live on the Switch UI for audit /
traceability. The card is purely a record; it must never feed back into the
agent's control flow.

Design invariants (do not violate):

* Go ONLY through :mod:`hermes_cli.kanban_db` functions — never raw SQL.
* Cards are an audit MIRROR, not dispatcher work: created with
  ``initial_status="running"``, ``created_by="matrix_coder"``, and **no
  assignee**, so the Kanban dispatcher (which claims only ``status='ready'``
  cards) never picks them up and re-executes the work.
* Cards are closed with the default ``expected_run_id=None`` — verified to work
  on never-claimed cards.
* DEFENSIVE + OPTIONAL: if :mod:`hermes_cli.kanban_db` can't be imported, or any
  kanban call raises, mirroring silently disables. It must NEVER break the agent
  hot path or the persona lifecycle. The ``KANBAN_AUDIT_ENABLED`` config flag can
  also disable it.

Tests inject a fake backend by reassigning the module-level :data:`_kb`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, List, Optional

from .config import load_config

logger = logging.getLogger(__name__)

# Defensive import: the kanban DB is optional. If it can't be imported (e.g.
# running the plugin standalone in a context without hermes_cli), mirroring is
# silently disabled. Exposed at module level so tests can inject a fake backend.
try:  # pragma: no cover - import availability is environment-dependent
    from hermes_cli import kanban_db as _kb
except Exception:  # pragma: no cover - defensive
    _kb = None  # type: ignore[assignment]


def is_enabled() -> bool:
    """True only when a kanban backend is importable AND the config flag is on."""
    if _kb is None:
        return False
    try:
        return bool(load_config().get("KANBAN_AUDIT_ENABLED", False))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("matrix_coder: kanban_audit.is_enabled config error: %s", exc, exc_info=True)
        return False


def open_card(
    role: str,
    lens: Optional[str],
    goal: str,
    session_id: Optional[str],
) -> Optional[str]:
    """Open a ``running`` audit card mirroring this invocation; return its id.

    Returns ``None`` if mirroring is disabled or any kanban call fails. The card
    is created with ``initial_status="running"``, ``created_by="matrix_coder"``,
    and NO assignee, so the dispatcher never claims and re-runs it.
    """
    if not is_enabled():
        return None
    try:
        title = f"matrix {role}{'/' + lens if lens else ''}: {goal[:60]}"
        conn = _kb.connect()
        try:
            card_id = _kb.create_task(
                conn,
                title=title,
                body=goal,
                created_by="matrix_coder",
                tenant="matrix_coder",
                session_id=session_id,
                initial_status="running",
                idempotency_key=uuid.uuid4().hex,
            )
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover - defensive
                pass
        return card_id
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: kanban_audit.open_card suppressed error: %s", exc)
        return None


def open_child_card(
    parent_id: str,
    role: str,
    lens: Optional[str],
    goal: str,
    session_id: Optional[str],
) -> Optional[str]:
    """Open a ``running`` child card under *parent_id* for one specialist dispatch.

    Returns the child card id, or ``None`` if mirroring is disabled or fails.
    The card is created with ``initial_status="running"``,
    ``created_by="matrix_coder"``, no assignee, and ``parents=[parent_id]`` so
    the Switch UI shows the invocation → dispatch hierarchy.

    For single-specialist invocations the caller should NOT create a child card
    (collapse to one card per spec). Use this only when there are two or more
    parallel/sequential dispatches within the same invocation.
    """
    if not is_enabled() or not parent_id:
        return None
    try:
        title = f"  ↳ {role}{'/' + lens if lens else ''}: {goal[:60]}"
        conn = _kb.connect()
        try:
            card_id = _kb.create_task(
                conn,
                title=title,
                body=goal,
                created_by="matrix_coder",
                tenant="matrix_coder",
                session_id=session_id,
                initial_status="running",
                idempotency_key=uuid.uuid4().hex,
                parents=[parent_id],
            )
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover - defensive
                pass
        return card_id
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "matrix_coder: kanban_audit.open_child_card suppressed error: %s", exc
        )
        return None


def close_card(
    card_id: Optional[str],
    summary: Optional[str],
    status: str = "done",
) -> None:
    """Close an audit card. ``status`` is ``"done"`` (complete) or ``"blocked"``.

    No-op when mirroring is disabled or ``card_id`` is falsy. Always defensive —
    never raises, so a kanban hiccup can't break the persona lifecycle.
    """
    if not is_enabled() or not card_id:
        return
    try:
        text = (summary or "")[:1000]
        conn = _kb.connect()
        try:
            if status == "blocked":
                _kb.block_task(conn, card_id, reason=text)
            else:
                _kb.complete_task(
                    conn,
                    card_id,
                    summary=text,
                    metadata={"source": "matrix_coder", "audit_mirror": True},
                )
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover - defensive
                pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("matrix_coder: kanban_audit.close_card suppressed error: %s", exc)
