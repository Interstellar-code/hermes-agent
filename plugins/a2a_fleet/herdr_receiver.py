"""Phase 3 — present a Herdr session as a Fleet peer reachable by ``fleet_send``.

This is the portless transport handler registered for ``mode: herdr_session``.
It fills the seam in :func:`client.send_message`, which forks to a handler for
herdr peers because they have no URL: they are reached over a local Unix socket
or an SSH bridge, never HTTP.

Three things make this different from the HTTP receivers, and all three are
deliberate:

**Session resolution, not a fixed address.** A herdr peer declares
``host_alias`` + ``workspace`` + ``agent_kind``, never a ``terminal_id`` — pane
handles change as the human works. So each dispatch resolves the session, and
resolving to more than one candidate is a refusal, never a pick. Guessing which
of two Claude sessions in a workspace the operator meant is the failure mode the
exact-identifier rule exists to prevent.

**No reply text.** An HTTP receiver returns the executor's answer. Herdr cannot:
the only way to read what the pane produced is scraping its content, which this
plan forbids outright. So the reply is a truthful dispatch envelope — what was
sent, where it landed, and what signal will mean completion — and it says so.

**Confirmation is not bypassed by routing.** Reaching a session through
``fleet_send`` rather than the tools does not lower the bar. Unless the operator
has explicitly set ``require_confirmation_for_mutations: false``, a dispatch
returns a confirmation token instead of typing into someone's live pane.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from . import herdr_binding, herdr_tools

log = logging.getLogger("a2a_fleet.herdr_receiver")

HERDR_SESSION_MODE = "herdr_session"


class HerdrRouteError(Exception):
    """Raised when a herdr peer cannot be routed to exactly one live session."""


async def _resolve_session(
    entry: Dict[str, Any], context_id: str
) -> Dict[str, Any]:
    """Pick the one session this peer refers to, or explain why we cannot.

    A context already bound to a session stays with it — continuity is by
    binding, not by re-matching attributes that may now describe a different
    pane. Only an unbound context searches.
    """
    host_alias = entry["host_alias"]
    workspace = entry["workspace"]
    agent_kind = entry["agent_kind"]

    bound = await herdr_tools._with_db(
        lambda conn: herdr_binding.get_binding_by_context(conn, context_id)
    )
    if bound and bound["host_alias"] == host_alias:
        inspected = await herdr_tools.herdr_inspect_session_handler(
            host_alias=host_alias, terminal_id=bound["terminal_id"]
        )
        if inspected.get("status") == "ok":
            return {"terminal_id": bound["terminal_id"], "session": inspected["session"]}
        # The bound pane is gone or no longer eligible. Say so rather than
        # silently re-homing this context onto a different session.
        raise HerdrRouteError(
            f"context {context_id} was bound to session {bound['terminal_id']} on "
            f"{host_alias}, which is no longer available "
            f"({inspected.get('status')}). Rebind deliberately: the work in that "
            "pane is not transferable by guessing."
        )

    listed = await herdr_tools.herdr_list_sessions_handler(
        host_alias=host_alias, workspace=workspace, agent_kind=agent_kind
    )
    if listed.get("status") not in (None, "ok"):
        raise HerdrRouteError(
            f"cannot list sessions on {host_alias!r}: {listed.get('reason') or listed}"
        )
    sessions = listed.get("sessions") or []
    if not sessions:
        raise HerdrRouteError(
            f"no {agent_kind!r} session found in {workspace!r} on {host_alias!r}"
        )
    if len(sessions) > 1:
        raise HerdrRouteError(
            f"{len(sessions)} {agent_kind!r} sessions match {workspace!r} on "
            f"{host_alias!r}: "
            + ", ".join(str(s.get("terminal_id")) for s in sessions)
            + ". Refusing to choose — target one with herdr_preview_action / "
            "herdr_request_action using its exact terminal_id."
        )
    session = sessions[0]
    return {"terminal_id": session.get("terminal_id"), "session": session}


async def send_to_herdr_session(
    agent_name: str,
    text: str,
    *,
    context_id: Optional[str] = None,
    timeout: float = 30.0,
    client: Any = None,
    **_kw: Any,
) -> Dict[str, str]:
    """Route one ``fleet_send`` to a Herdr session peer.

    Returns the same ``{"reply", "context_id"}`` shape as the HTTP path so
    ``fleet_send`` needs no herdr-specific branch.
    """
    from .fleet_config import get_agent  # noqa: PLC0415 — lazy, avoids import cycle.

    entry = get_agent(agent_name)
    context_id = context_id or f"herdr-{uuid.uuid4()}"
    host_alias = entry["host_alias"]

    try:
        resolved = await _resolve_session(entry, context_id)
    except HerdrRouteError as exc:
        return {"reply": f"[herdr] {exc}", "context_id": context_id}

    terminal_id = resolved["terminal_id"]
    session = resolved["session"]

    if await herdr_tools._with_db(
        lambda conn: herdr_binding.automation_blocked(conn, host_alias, terminal_id)
    ):
        return {
            "reply": (
                f"[herdr] session {terminal_id} on {host_alias} is held by a human; "
                "Fleet automation is paused for it. Release with "
                "herdr_release_human_takeover when the human is done."
            ),
            "context_id": context_id,
        }

    # Bind the context to this session before anything is sent, so a dispatch
    # that dies mid-flight still leaves the association discoverable.
    await herdr_tools._with_db(
        lambda conn: herdr_binding.upsert_binding(
            conn,
            host_alias=host_alias,
            terminal_id=terminal_id,
            fleet_context_id=context_id,
            pane_id=session.get("pane_id"),
            agent_kind=session.get("agent_kind"),
            workspace_canonical_path=session.get("cwd"),
            transport_kind=entry.get("transport"),
            completion_state="pending",
        )
    )

    preview = await herdr_tools.herdr_preview_action_handler(
        host_alias=host_alias,
        terminal_id=terminal_id,
        action=text,
        summary=f"fleet_send from context {context_id}",
    )
    if preview.get("status") != "preview":
        return {
            "reply": f"[herdr] cannot dispatch: {preview.get('reason') or preview}",
            "context_id": context_id,
        }

    cfg = herdr_tools._herdr_cfg()
    if cfg.get("require_confirmation_for_mutations", True):
        return {
            "reply": (
                f"[herdr] confirmation required before writing into a live session.\n"
                f"session: {terminal_id} ({session.get('agent_kind')}) on {host_alias}\n"
                f"workspace: {session.get('cwd')}\n"
                f"text: {text}\n"
                f"confirm with herdr_request_action(host_alias={host_alias!r}, "
                f"terminal_id={terminal_id!r}, confirmation_token="
                f"{preview['confirmation_token']!r})\n"
                f"token expires in {preview['expires_in_seconds']}s and is refused if "
                f"the session produces output first."
            ),
            "context_id": context_id,
        }

    sent = await herdr_tools.herdr_request_action_handler(
        host_alias=host_alias,
        terminal_id=terminal_id,
        confirmation_token=preview["confirmation_token"],
        wait_timeout_ms=int(entry.get("wait_timeout_ms") or 0),
    )
    if sent.get("status") != "submitted":
        return {
            "reply": f"[herdr] dispatch failed ({sent.get('status')}): "
                     f"{sent.get('reason') or ''}".strip(),
            "context_id": context_id,
        }

    return {
        "reply": (
            f"[herdr] dispatched to {terminal_id} ({session.get('agent_kind')}) on "
            f"{host_alias}; completion_state={sent.get('completion_state')}. "
            f"Completion is signalled by {herdr_tools.COMPLETION_SIGNAL} — the "
            f"session's own output is never read back, so this is a dispatch "
            f"receipt, not the session's answer."
        ),
        "context_id": context_id,
    }


def register_herdr_route() -> None:
    """Install the portless transport handler for ``mode: herdr_session``."""
    from .client import register_portless_handler  # noqa: PLC0415 — avoids cycle.

    register_portless_handler(HERDR_SESSION_MODE, send_to_herdr_session)
    log.debug("a2a_fleet: registered portless route for %s", HERDR_SESSION_MODE)
