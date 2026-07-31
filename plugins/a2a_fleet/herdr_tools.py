"""Agent-facing Herdr session tools for the a2a_fleet plugin.

Discovery and inspection — ``herdr_status``, ``herdr_list_sessions``,
``herdr_inspect_session`` — are strictly read-only.

Exactly ONE operation here mutates a live session: ``herdr_request_action``
submits a previewed, confirmed prompt through Herdr's own ``agent prompt``
verb. Pane-level verbs and keystroke verbs remain deliberately unwrapped —
they bypass the agent-session identity boundary, and ``agent prompt`` takes
``(target, text)`` with no key list, so no arbitrary-key route exists at all.

Every mutation passes the same gates in this order: exact ``terminal_id``,
per-host ``allow_actions``, human takeover, single-use confirmation token,
revision guard, durable binding, then an audit record written BEFORE the
submission. Nothing here ever retries: Herdr issues no request ID, so a
delivered prompt and a lost one are indistinguishable afterwards.

Agent kinds: the path is kind-neutral by construction — Herdr owns the
per-runtime submission encoding inside ``agent prompt``, so there is nothing
here to special-case. ``claude``, ``codex`` and ``opencode`` have been driven
end-to-end against real panes on herdr 0.7.5 / protocol 17 (see
``VERIFIED_AGENT_KINDS``). A host may set ``supported_agent_kinds`` to refuse
kinds it has not verified; omitting it restricts nothing.

One trap is NOT kind-specific and bites every kind: for a short window after
``herdr agent start``, Herdr accepts a prompt, reports success, and delivers
nothing. ``interactive_ready`` is true and ``agent_status`` is ``idle`` in that
window, and ``state_change_seq`` advances on its own as detection settles, so
none of those separate it from a working session. ``_target_ready`` refuses
sessions with no ``agent_session`` for exactly this reason.

Every handler returns a plain dict and never raises, mirroring
:mod:`fleet_tools`: the calling agent can surface the string verbatim in chat
without exception handling.

Dispatch shape: ``registry.dispatch()`` calls ``handler(args, **kwargs)`` — the
WHOLE args dict lands in the first positional and ``task_id`` is injected as a
kwarg. Each handler unwraps that dict (mirrors ``fleet_send_handler``) so the
tools work on the live gateway path while still tolerating direct kwarg-style
calls from tests.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import herdr_binding
from .herdr_capability import check_herdr_capability
from .herdr_client import (
    PROMPT_NOT_SUBMITTED,
    PROMPT_REJECTED_BEFORE_SUBMISSION,
    PROMPT_WAIT_TIMEOUT,
    HerdrClient,
    HerdrError,
    HerdrNotFound,
    normalize_agent_record,
)

log = logging.getLogger("a2a_fleet.herdr_tools")

# The completion signal Fleet accepts. Named here so the preview payload can
# state it up front: the operator confirming an action is told exactly what
# will be treated as "done", and it is never silence or an idle prompt.
COMPLETION_SIGNAL = "herdr agent wait <terminal_id> --until done"


def _revision_guard_state(revision: Any) -> str:
    """Say whether the staleness guard can actually see change in this session.

    ``revision`` is NOT an output counter, despite riding on the
    ``pane_output_changed`` event. In Herdr 0.7.4 it is incremented in exactly
    three places, none of them terminal output:

      src/terminal/state.rs:198   stripped terminal title changed
      src/app/actions.rs:1083     metadata-token expiry
      src/app/api/panes.rs:1408   metadata-token patch (report-metadata)

    So it tracks *presentation*, not work. Agents that rewrite their title as
    they run (Claude Code, OpenCode) move it and the guard is meaningful; a
    plain shell never moves it and the guard is blind — verified on
    2026-07-29, where a throwaway bash pane stayed at revision 0 across
    delivered text and a stale token was therefore accepted.

    Reporting "blind" is the point: a guard that silently sees nothing is worse
    than no guard, because the operator believes they are protected. When it is
    blind, the real protections are the single-use token, its short TTL, and
    the human who confirmed it.
    """
    if revision is None:
        return "unavailable: session reports no revision"
    if revision == 0:
        return (
            "blind: this session has never bumped its revision (it does not set a "
            "terminal title), so a change between preview and send cannot be "
            "detected — rely on the token TTL and your own confirmation"
        )
    return "active: revision moves when this session updates its title or metadata"


def _terminal_id_problem(terminal_id: str) -> Optional[Dict[str, str]]:
    """Return a structured complaint for a malformed ``terminal_id``, else None.

    Malformed input must not reach the subprocess: a NUL byte made
    ``create_subprocess_exec`` raise ``ValueError('embedded null byte')``, which
    surfaced as an opaque ``unexpected error`` instead of a validation status.
    Whitespace-padded ids are rejected rather than trimmed — silently
    normalizing an identifier is exactly the loose matching these tools exist to
    refuse — but the caller is told what to strip.
    """
    if terminal_id != terminal_id.strip():
        return {
            "reason": "terminal_id has leading or trailing whitespace",
            "hint": f"pass {terminal_id.strip()!r} — identifiers are never trimmed for you",
        }
    if any(ch == "\x00" or (ord(ch) < 32) for ch in terminal_id):
        return {
            "reason": "terminal_id contains control characters",
            "hint": "use the exact term_... handle from herdr_list_sessions",
        }
    return None


def _herdr_cfg() -> Dict[str, Any]:
    """Return the ``fleet.herdr`` block, or an empty feature-off default.

    Config load failures are swallowed into the feature-off shape on purpose: a
    broken or absent fleet.yaml must degrade these tools to a structured
    "unconfigured" response, never take down the calling agent's turn.
    """
    try:
        from .fleet_config import load_fleet  # noqa: PLC0415 — lazy, avoids import cycle.

        return load_fleet().get("herdr") or {}
    except Exception:  # noqa: BLE001 — tools must never crash the agent loop.
        log.debug("herdr_tools: fleet.herdr config unavailable", exc_info=True)
        return {}


def _host_cfg(cfg: Dict[str, Any], host_alias: str) -> Optional[Dict[str, Any]]:
    return (cfg.get("hosts") or {}).get(host_alias)


def _client_for(host_cfg: Dict[str, Any]) -> HerdrClient:
    """Build a client for ``host_cfg``; remote is just an argv prefix."""
    ssh_target = host_cfg.get("ssh_target") if host_cfg.get("transport") == "ssh_bridge" else None
    return HerdrClient(ssh_target=ssh_target)


def _workspace_allowed(cwd: Optional[str], allowed: List[str]) -> bool:
    """True when ``cwd`` resolves inside one of the ``allowed`` workspace roots.

    Uses resolved-path containment rather than string prefixes so that
    ``/srv/workspaces/project-a-evil`` does not match an allowlist entry of
    ``/srv/workspaces/project-a``.
    """
    if not cwd:
        return False
    try:
        target = Path(cwd).resolve()
    except (OSError, ValueError):
        return False
    for root in allowed:
        try:
            if target.is_relative_to(Path(root).resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


async def _resolve_host(host_alias: Any) -> Dict[str, Any]:
    """Shared preflight: validate the alias and run the capability probe.

    Returns either ``{"ok": True, "host_cfg": ..., "capability": ...}`` or a
    ready-to-return structured error dict under ``{"ok": False, "result": ...}``.
    """
    if not isinstance(host_alias, str) or not host_alias.strip():
        return {"ok": False, "result": {"error": "host_alias is required and must be a string"}}

    cfg = _herdr_cfg()
    host_cfg = _host_cfg(cfg, host_alias)
    if host_cfg is None:
        known = sorted((cfg.get("hosts") or {}).keys())
        return {
            "ok": False,
            "result": {
                "status": "unknown_host_alias",
                "host_alias": host_alias,
                "known_hosts": known,
                "reason": (
                    f"host_alias {host_alias!r} is not defined in fleet.herdr.hosts"
                    + (f"; known aliases: {known}" if known else "; no hosts are configured")
                ),
            },
        }

    capability = await check_herdr_capability(host_alias, cfg)
    if capability.get("status") != "ok":
        return {"ok": False, "result": capability}

    return {"ok": True, "host_cfg": host_cfg, "capability": capability, "cfg": cfg}


async def herdr_status_handler(
    host_alias: Any = "",
    **_injected: Any,
) -> Dict[str, Any]:
    """Report Herdr version, protocol, transport, and reachability for a host.

    This is the capability probe surfaced as a tool. It is also the only place
    integration install/load state is reported — as diagnostics, never as a gate.
    """
    if isinstance(host_alias, dict):
        host_alias = host_alias.get("host_alias", "") or ""

    try:
        resolved = await _resolve_host(host_alias)
    except Exception as exc:  # noqa: BLE001 — tools must never crash the agent loop.
        log.exception("herdr_status: unexpected error for %r", host_alias)
        return {"error": f"unexpected error: {exc}"}

    if not resolved["ok"]:
        return resolved["result"]

    result = dict(resolved["capability"])
    host_cfg = resolved["host_cfg"]
    result["allowed_workspaces"] = list(host_cfg.get("allowed_workspaces") or [])
    return result


async def herdr_list_sessions_handler(
    host_alias: Any = "",
    workspace: str = "",
    agent_kind: str = "",
    **_injected: Any,
) -> Dict[str, Any]:
    """List Herdr-managed agent sessions visible on ``host_alias``.

    Sessions outside the host's ``allowed_workspaces`` are filtered out and
    counted, never returned — the allowlist is enforced here, not left to the
    caller. Filtering is on ``cwd``/``workspace_id``; ``foreground_cwd`` is
    deliberately ignored (it tracks a transient child process and diverges).
    """
    if isinstance(host_alias, dict):
        params = host_alias
        host_alias = params.get("host_alias", "") or ""
        workspace = params.get("workspace", "") or workspace
        agent_kind = params.get("agent_kind", "") or agent_kind

    try:
        resolved = await _resolve_host(host_alias)
        if not resolved["ok"]:
            return resolved["result"]

        host_cfg = resolved["host_cfg"]
        allowed = list(host_cfg.get("allowed_workspaces") or [])
        client = _client_for(host_cfg)
        raw = await client.list_agents()

        sessions: List[Dict[str, Any]] = []
        denied = 0
        for record in (raw.get("agents") or []):
            normalized = normalize_agent_record(record)
            if not _workspace_allowed(normalized.get("cwd"), allowed):
                denied += 1
                continue
            if workspace and normalized.get("cwd") != workspace:
                continue
            if agent_kind and normalized.get("agent_kind") != agent_kind:
                continue
            sessions.append(normalized)

        return {
            "status": "ok",
            "host_alias": host_alias,
            "sessions": sessions,
            "count": len(sessions),
            "filtered_out_by_allowlist": denied,
        }
    except HerdrError as exc:
        log.warning("herdr_list_sessions: herdr error on %r: %s", host_alias, exc)
        return {"status": "herdr_error", "host_alias": host_alias, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 — tools must never crash the agent loop.
        log.exception("herdr_list_sessions: unexpected error for %r", host_alias)
        return {"error": f"unexpected error: {exc}"}


async def herdr_inspect_session_handler(
    host_alias: Any = "",
    terminal_id: str = "",
    **_injected: Any,
) -> Dict[str, Any]:
    """Inspect exactly one Herdr session by its stable ``terminal_id``.

    Exact identifier only — there is no "first matching pane" fallback, and the
    ``agent`` kind label is NOT accepted as an identifier because it is not
    unique (two Claude panes collide). A session outside the host's
    ``allowed_workspaces`` is reported as denied rather than returned.
    """
    if isinstance(host_alias, dict):
        params = host_alias
        host_alias = params.get("host_alias", "") or ""
        terminal_id = params.get("terminal_id", "") or terminal_id

    if not isinstance(terminal_id, str) or not terminal_id.strip():
        return {"error": "terminal_id is required (the stable term_... handle, not the agent label)"}

    problem = _terminal_id_problem(terminal_id)
    if problem is not None:
        return {
            "status": "invalid_terminal_id",
            "host_alias": host_alias if isinstance(host_alias, str) else "",
            "terminal_id": terminal_id,
            **problem,
        }

    try:
        resolved = await _resolve_host(host_alias)
        if not resolved["ok"]:
            return resolved["result"]

        host_cfg = resolved["host_cfg"]
        allowed = list(host_cfg.get("allowed_workspaces") or [])
        client = _client_for(host_cfg)

        # EXACT IDENTIFIER ENFORCEMENT, done on OUR side of the boundary.
        #
        # Herdr 0.7.5 stopped accepting terminal ids as `agent` targets ("targets
        # accept unique agent names and pane ids that currently host agents") —
        # `agent get <terminal_id>` now returns agent_not_found. terminal_id is
        # still carried in every record; it is simply no longer addressable.
        #
        # So resolution happens here: list the sessions and match terminal_id
        # exactly. That keeps terminal_id as the identity key callers must use —
        # agent kind labels stay rejected because they are not unique across
        # panes — while addressing Herdr by the pane_id it now requires. It also
        # removes the dependency on Herdr's own loose target resolution, which
        # used to accept labels and pick whichever pane matched.
        try:
            listing = await client.list_agents()
        except HerdrNotFound:
            listing = {"agents": []}

        records = [normalize_agent_record(r) for r in (listing.get("agents") or [])]
        matches = [r for r in records if r.get("terminal_id") == terminal_id]

        if not matches:
            # Distinguish "no such session" from "you passed a label or pane id".
            # The second is the ambiguous selection this tool exists to prevent,
            # and the caller needs to be told which mistake they made.
            by_other_handle = [
                r for r in records
                if terminal_id in (r.get("agent_kind"), r.get("pane_id"))
            ]
            if by_other_handle:
                return {
                    "status": "ambiguous_identifier",
                    "host_alias": host_alias,
                    "requested": terminal_id,
                    "candidate_terminal_ids": [r.get("terminal_id") for r in by_other_handle],
                    "reason": (
                        f"{terminal_id!r} is an agent kind label or pane id, not a "
                        f"terminal_id. Those are not unique across panes and are rejected "
                        f"as identifiers. Use herdr_list_sessions to get the exact "
                        f"terminal_id."
                    ),
                }
            return {
                "status": "not_found",
                "host_alias": host_alias,
                "terminal_id": terminal_id,
                "reason": f"no Herdr session with terminal_id {terminal_id!r}",
            }

        normalized = matches[0]

        if not _workspace_allowed(normalized.get("cwd"), allowed):
            return {
                "status": "workspace_denied",
                "host_alias": host_alias,
                "terminal_id": terminal_id,
                "reason": (
                    f"session cwd {normalized.get('cwd')!r} is outside "
                    f"host {host_alias!r}'s allowed_workspaces"
                ),
            }

        return {
            "status": "ok",
            "host_alias": host_alias,
            "session": normalized,
        }
    except HerdrError as exc:
        log.warning("herdr_inspect_session: herdr error on %r: %s", host_alias, exc)
        return {"status": "herdr_error", "host_alias": host_alias, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 — tools must never crash the agent loop.
        log.exception("herdr_inspect_session: unexpected error for %r", host_alias)
        return {"error": f"unexpected error: {exc}"}


# ---------------------------------------------------------------------------
# Phase 2 — confirmation-gated actions and human takeover
# ---------------------------------------------------------------------------


def _actions_allowed(cfg: Dict[str, Any], host_cfg: Dict[str, Any]) -> Optional[str]:
    """Return a refusal reason if mutations are not enabled for this host.

    ``fleet.herdr.read_only_default`` is true unless the operator says
    otherwise, and even then each host must opt in with ``allow_actions:
    true``. Writing into a live pane someone is working in is a trust boundary;
    it stays shut until opened deliberately, per host, never globally by
    accident.
    """
    if host_cfg.get("allow_actions") is True:
        return None
    if cfg.get("read_only_default", True):
        return (
            "actions are disabled: set fleet.herdr.hosts.<alias>.allow_actions: true "
            "to permit confirmed mutations for this host"
        )
    return None


async def _submit_supported(host_cfg: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Refuse before touching a session if this Herdr cannot submit prompts.

    ``agent prompt`` — the only agent-scoped verb that writes text AND submits
    it — landed in Herdr 0.7.5. On 0.7.4 there is no agent-scoped submit route
    at all, and the pane-level keystroke verbs are deliberately out of bounds
    because they bypass the agent-session identity boundary.

    Checked here rather than discovered at submission time. Against 0.7.4 the
    wrapper previously invoked a subcommand the binary did not have, and the
    failure surfaced only after the confirmation token had been spent on a real
    session — an operator left to wonder whether a prompt had landed.
    """
    if await _client_for(host_cfg).supports_submit():
        return None
    return {
        "status": "submission_unsupported",
        "reason": (
            "this Herdr build has no agent-scoped submit verb (`agent prompt`, "
            "added in Herdr 0.7.5), so a prompt could only be typed in, never "
            "submitted"
        ),
        "required": "herdr >= 0.7.5",
        "note": (
            "Upgrading also moves the wire protocol (16 -> 18), so the pinned "
            "protocol in herdr_client must be re-verified at the same time."
        ),
    }


# Agent kinds driven end-to-end through this code path against a real pane.
# Everything here was verified on herdr 0.7.5 / protocol 17: discovery, exact-id
# inspection, preview, confirmed submit, delivery observed in the pane, replay
# refused, human takeover blocking both verbs, and audit ordering.
VERIFIED_AGENT_KINDS = ("claude", "codex", "opencode")


def _kind_allowed(
    session: Dict[str, Any], host_cfg: Dict[str, Any]
) -> Optional[Dict[str, str]]:
    """Enforce an OPTIONAL per-host ``supported_agent_kinds`` allowlist.

    Herdr 0.7.5 detects 21 agent kinds and will add more. The submission path
    is genuinely kind-neutral — Herdr owns the per-runtime encoding inside
    ``agent prompt`` — so this is not needed for correctness, and when the key
    is absent nothing is restricted. It exists so an operator can decline to
    treat every future kind as proven safe by default.

    ``VERIFIED_AGENT_KINDS`` records what has actually been driven end-to-end;
    it is documentation, not the default policy.
    """
    allowed = host_cfg.get("supported_agent_kinds")
    if not allowed:
        return None
    kind = session.get("agent_kind") or ""
    if kind in allowed:
        return None
    return {
        "status": "agent_kind_not_allowed",
        "reason": (
            f"agent kind {kind!r} is not in this host's supported_agent_kinds; "
            "add it there once you have verified it end-to-end"
        ),
        "agent_kind": kind,
        "supported_agent_kinds": ", ".join(allowed),
        "verified_kinds": ", ".join(VERIFIED_AGENT_KINDS),
    }


def _target_ready(session: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Refuse a target that has not yet established its own agent session.

    A freshly started agent has a startup window in which ``agent prompt``
    accepts the call, returns success, and delivers NOTHING. Verified live on
    2026-07-31 against Herdr 0.7.5 on both ``codex`` and ``opencode``: prompt
    sent seconds after ``agent start`` returned ok with the pane composer
    untouched and the agent's context unconsumed.

    None of the obvious signals separate that window from a working session:

    * ``interactive_ready`` is ``true`` in it (that is what ``agent start``
      waits for, and it is not enough);
    * ``agent_status`` reads ``idle``, the same as a healthy waiting session;
    * ``state_change_seq`` ADVANCES during it — the cold codex went 55 -> 58
      while its startup detection settled — so the post-submit evidence check
      that guards the recovery path cannot catch this one either.

    ``agent_session`` is the discriminator: absent until the agent actually
    establishes a session, present on every session that accepts prompts. This
    is deliberately NOT agent-kind-aware — the window reproduced identically on
    both kinds, so a per-kind table would encode a coincidence.

    Consequence of refusing here: Fleet will not drive an agent that has never
    taken a turn. That is the intended scope anyway — fleet peers are
    long-lived working sessions, and the first turn belongs to the operator.
    """
    if session.get("agent_session_id"):
        return None
    return {
        "status": "submission_target_not_ready",
        "reason": (
            "this agent has not established a session yet (no agent_session), so "
            "Herdr accepts a prompt for it and silently drops it — give it its "
            "first turn interactively, then retry"
        ),
        "agent_kind": session.get("agent_kind") or "",
        "agent_status": str(session.get("agent_status")),
        "retry": "after the agent has taken at least one turn",
    }


async def _with_db(fn: Any) -> Any:
    """Run one unit of SQLite work on a worker thread, connection and all.

    The connection is opened AND closed inside that same thread on purpose:
    sqlite3 objects are thread-bound, so a connection created in one
    ``to_thread`` worker and closed on the event loop raises ProgrammingError.
    Opening per unit is cheap and removes the cross-thread lifetime entirely —
    cheaper than the alternative of a long-lived connection plus a lock, and it
    keeps the sync store off the event loop (the #169 stall lesson).
    """

    def _run() -> Any:
        conn = herdr_binding.connect()
        try:
            return fn(conn)
        finally:
            conn.close()

    return await asyncio.to_thread(_run)


async def _audit(**kw: Any) -> None:
    await _with_db(lambda conn: herdr_binding.record_audit(conn, **kw))


async def herdr_preview_action_handler(
    params: Any = None,
    host_alias: str = "",
    terminal_id: str = "",
    action: str = "",
    summary: str = "",
    _injected: Any = None,
    **_kw: Any,
) -> Dict[str, Any]:
    """Describe exactly one pending mutation and mint a confirmation token.

    Mutates nothing. The token is single-use, short-lived, bound to this one
    session, and carries the session's ``revision`` at preview time so the
    action cannot land on a pane that has moved since the operator looked.
    """
    if isinstance(params, dict):
        host_alias = params.get("host_alias", "") or host_alias
        terminal_id = params.get("terminal_id", "") or terminal_id
        action = params.get("action", "") or action
        summary = params.get("summary", "") or summary

    if not isinstance(action, str) or not action.strip():
        return {"error": "action is required (the literal text to send to the session)"}

    try:
        inspected = await herdr_inspect_session_handler(
            host_alias=host_alias, terminal_id=terminal_id
        )
        if inspected.get("status") != "ok":
            return inspected

        cfg = _herdr_cfg()
        host_cfg = _host_cfg(cfg, host_alias) or {}
        refusal = _actions_allowed(cfg, host_cfg)
        if refusal:
            return {
                "status": "actions_disabled",
                "host_alias": host_alias,
                "terminal_id": terminal_id,
                "reason": refusal,
            }

        unsupported = await _submit_supported(host_cfg)
        if unsupported:
            return {"host_alias": host_alias, "terminal_id": terminal_id, **unsupported}

        if await _with_db(
            lambda conn: herdr_binding.automation_blocked(conn, host_alias, terminal_id)
        ):
            return {
                "status": "human_takeover",
                "host_alias": host_alias,
                "terminal_id": terminal_id,
                "reason": "a human holds this session; release the takeover first",
            }

        session = inspected["session"]
        bad_kind = _kind_allowed(session, host_cfg)
        if bad_kind is not None:
            return {**bad_kind, "host_alias": host_alias, "terminal_id": terminal_id}
        not_ready = _target_ready(session)
        if not_ready is not None:
            # Refuse before minting a token: a token bound to a session that
            # silently drops prompts would be spent on nothing.
            return {**not_ready, "host_alias": host_alias, "terminal_id": terminal_id}
        issued = await _with_db(
            lambda conn: herdr_binding.issue_token(
                conn,
                host_alias=host_alias,
                terminal_id=terminal_id,
                pane_id=session.get("pane_id"),
                revision=session.get("revision"),
                action=action,
                summary=summary,
            )
        )
        await _audit(
            event="preview",
            host_alias=host_alias,
            terminal_id=terminal_id,
            action_id=issued["action_id"],
            detail=summary or action[:200],
        )

        return {
            "status": "preview",
            "host_alias": host_alias,
            "terminal_id": terminal_id,
            "pane_id": session.get("pane_id"),
            "agent_kind": session.get("agent_kind"),
            "workspace": session.get("cwd"),
            "action": action,
            "summary": summary,
            "revision_at_preview": session.get("revision"),
            "revision_guard": _revision_guard_state(session.get("revision")),
            "completion_signal": COMPLETION_SIGNAL,
            "confirmation_token": issued["token"],
            "action_id": issued["action_id"],
            "expires_in_seconds": herdr_binding.TOKEN_TTL_SECONDS,
            "note": (
                "Single use. herdr_request_action refuses if the session's revision "
                "has moved since this preview — see revision_guard for whether that "
                "check can see anything in this session."
            ),
        }
    except HerdrError as exc:
        log.warning("herdr_preview_action: herdr error on %r: %s", host_alias, exc)
        return {"status": "herdr_error", "host_alias": host_alias, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 — tools must never crash the agent loop.
        log.exception("herdr_preview_action: unexpected error for %r", host_alias)
        return {"error": f"unexpected error: {exc}"}


async def herdr_request_action_handler(
    params: Any = None,
    host_alias: str = "",
    terminal_id: str = "",
    confirmation_token: str = "",
    wait_timeout_ms: int = 0,
    _injected: Any = None,
    **_kw: Any,
) -> Dict[str, Any]:
    """Perform one previewed, confirmed action against exactly one session.

    Never retries. ``herdr agent send`` carries no request ID, so a delivered
    action and a lost one look identical afterwards; an unknown outcome is
    recorded as unknown and handed to the operator rather than repeated.
    """
    if isinstance(params, dict):
        host_alias = params.get("host_alias", "") or host_alias
        terminal_id = params.get("terminal_id", "") or terminal_id
        confirmation_token = params.get("confirmation_token", "") or confirmation_token
        wait_timeout_ms = params.get("wait_timeout_ms", wait_timeout_ms) or 0

    if not isinstance(confirmation_token, str) or not confirmation_token.strip():
        return {"error": "confirmation_token is required — run herdr_preview_action first"}

    try:
        inspected = await herdr_inspect_session_handler(
            host_alias=host_alias, terminal_id=terminal_id
        )
        if inspected.get("status") != "ok":
            return inspected

        cfg = _herdr_cfg()
        host_cfg = _host_cfg(cfg, host_alias) or {}
        refusal = _actions_allowed(cfg, host_cfg)
        if refusal:
            return {
                "status": "actions_disabled",
                "host_alias": host_alias,
                "terminal_id": terminal_id,
                "reason": refusal,
            }

        unsupported = await _submit_supported(host_cfg)
        if unsupported:
            return {"host_alias": host_alias, "terminal_id": terminal_id, **unsupported}

        if await _with_db(
            lambda conn: herdr_binding.automation_blocked(conn, host_alias, terminal_id)
        ):
            return {
                "status": "human_takeover",
                "host_alias": host_alias,
                "terminal_id": terminal_id,
                "reason": "a human holds this session; automation is paused",
            }

        session = inspected["session"]
        bad_kind = _kind_allowed(session, host_cfg)
        if bad_kind is not None:
            return {**bad_kind, "host_alias": host_alias, "terminal_id": terminal_id}
        not_ready = _target_ready(session)
        if not_ready is not None:
            # Before consuming the token: the single-use token must not be
            # burned on a session that would swallow the prompt silently.
            return {**not_ready, "host_alias": host_alias, "terminal_id": terminal_id}
        claim = await _with_db(
            lambda conn: herdr_binding.consume_token(
                conn,
                token=confirmation_token,
                host_alias=host_alias,
                terminal_id=terminal_id,
            )
        )
        if not claim["ok"]:
            await _audit(
                event="rejected",
                host_alias=host_alias,
                terminal_id=terminal_id,
                detail=claim["reason"],
            )
            return {
                "status": claim["reason"],
                "host_alias": host_alias,
                "terminal_id": terminal_id,
                "reason": "confirmation token is not usable; preview again",
            }

        token_row = claim["row"]
        action_id = token_row["action_id"]

        # Optimistic concurrency, with a documented blind spot. The token is
        # already spent, so a moved revision fails closed rather than
        # re-arming. But `revision` tracks title/metadata changes, NOT output
        # (see _revision_guard_state for the three increment sites), so it
        # detects change only in sessions that rewrite their title. The
        # response carries revision_guard so a blind check is never mistaken
        # for a passed one.
        live_revision = session.get("revision")
        if token_row["revision"] is not None and live_revision != token_row["revision"]:
            await _audit(
                event="rejected_stale",
                host_alias=host_alias,
                terminal_id=terminal_id,
                action_id=action_id,
                detail=f"revision {token_row['revision']} -> {live_revision}",
            )
            return {
                "status": "revision_moved",
                "host_alias": host_alias,
                "terminal_id": terminal_id,
                "revision_at_preview": token_row["revision"],
                "revision_now": live_revision,
                "reason": (
                    "the session produced output since the preview; the token is spent "
                    "— inspect the session and preview again if the action still applies"
                ),
            }

        await _with_db(
            lambda conn: herdr_binding.upsert_binding(
                conn,
                host_alias=host_alias,
                terminal_id=terminal_id,
                pane_id=session.get("pane_id"),
                agent_kind=session.get("agent_kind"),
                workspace_canonical_path=session.get("cwd"),
                revision_at_preview=token_row["revision"],
                action_id=action_id,
                transport_kind=host_cfg.get("transport"),
                completion_state="pending",
            )
        )
        # Audit the intent BEFORE the submission. If this process dies mid-call
        # the trail still shows an action whose outcome is unknown — which is
        # the honest state. An audit written only on success would leave a
        # submitted prompt with no record of it at all.
        await _audit(
            event="submit_attempted",
            host_alias=host_alias,
            terminal_id=terminal_id,
            action_id=action_id,
            detail=token_row["action"][:500],
        )

        client = _client_for(host_cfg)
        try:
            # Addressed by pane_id: Herdr 0.7.5 no longer accepts terminal ids
            # as agent targets. The resolver above already proved this pane_id
            # belongs to the exact terminal_id that was previewed and confirmed.
            herdr_target = session.get("pane_id") or terminal_id
            await client.submit_prompt(herdr_target, token_row["action"])
        except HerdrError as exc:
            # Herdr names the failures that happened before it wrote anything
            # (see PROMPT_REJECTED_BEFORE_SUBMISSION). Those are clean
            # rejections: nothing is sitting in the composer, so say so rather
            # than leaving the operator to check by hand.
            code = getattr(exc, "code", None)
            if code in PROMPT_WAIT_TIMEOUT:
                # Herdr stopped watching; it did NOT say the Enter failed.
                # Resolve with evidence rather than pressing Enter again, which
                # on an already-submitted prompt would be a second submission.
                advanced_to = await _seq_advanced(
                    client,
                    session.get("pane_id") or terminal_id,
                    terminal_id,
                    session.get("state_change_seq"),
                )
                if advanced_to is not None:
                    await _audit(
                        event="submitted_after_wait_timeout",
                        host_alias=host_alias,
                        terminal_id=terminal_id,
                        action_id=action_id,
                        detail=(
                            f"state_change_seq {session.get('state_change_seq')} "
                            f"-> {advanced_to}"
                        ),
                    )
                    await _with_db(
                        lambda conn: herdr_binding.upsert_binding(
                            conn,
                            host_alias=host_alias,
                            terminal_id=terminal_id,
                            completion_state="pending",
                        )
                    )
                    return {
                        "status": "submitted",
                        "host_alias": host_alias,
                        "terminal_id": terminal_id,
                        "pane_id": session.get("pane_id"),
                        "action_id": action_id,
                        "completion_state": "pending",
                        "completion_signal": COMPLETION_SIGNAL,
                        "submission": (
                            "Herdr's wait budget expired before the agent reached an "
                            "observable status, but the session did change state, so "
                            "the prompt was submitted. Nothing was re-sent."
                        ),
                    }
                # No movement: treat exactly like a stall — Enter once, then
                # demand evidence again.
                recovered = await _recover_stalled_submission(
                    client, session, host_alias, terminal_id, action_id
                )
                if recovered is not None:
                    return recovered
            if code in PROMPT_NOT_SUBMITTED:
                # Herdr's scheduled Enter did not land, so our previewed text is
                # sitting in the composer. Press Enter once — the key is fixed
                # and the text is never re-sent, so this completes the action
                # rather than repeating it, and cannot duplicate a prompt.
                recovered = await _recover_stalled_submission(
                    client, session, host_alias, terminal_id, action_id
                )
                if recovered is not None:
                    return recovered
                # Herdr saw no state change after submitting: the text went in
                # but the Enter did not take, so a draft is sitting in the
                # composer. Verified live on 2026-07-30 — without --wait this
                # exact case returned "submitted".
                await _audit(
                    event="draft_not_submitted", host_alias=host_alias,
                    terminal_id=terminal_id, action_id=action_id,
                    detail=str(exc)[:300],
                )
                await _with_db(
                    lambda conn: herdr_binding.upsert_binding(
                        conn, host_alias=host_alias, terminal_id=terminal_id,
                        completion_state="failed",
                    )
                )
                return {
                    "status": "draft_inserted_not_submitted",
                    "host_alias": host_alias,
                    "terminal_id": terminal_id,
                    "action_id": action_id,
                    "mutation": "text inserted, NOT submitted — it is in the composer",
                    "reason": str(exc),
                    "retry": "never — resubmitting would duplicate the draft",
                }
            rejected_cleanly = code in PROMPT_REJECTED_BEFORE_SUBMISSION
            await _audit(
                event="submission_rejected" if rejected_cleanly else "submission_unknown",
                host_alias=host_alias,
                terminal_id=terminal_id,
                action_id=action_id,
                detail=f"{getattr(exc, 'code', '?')}: {exc}"[:500],
            )
            await _with_db(
                lambda conn: herdr_binding.upsert_binding(
                    conn,
                    host_alias=host_alias,
                    terminal_id=terminal_id,
                    completion_state="failed",
                )
            )
            if rejected_cleanly:
                return {
                    "status": "submission_rejected",
                    "host_alias": host_alias,
                    "terminal_id": terminal_id,
                    "action_id": action_id,
                    "herdr_error_code": getattr(exc, "code", None),
                    "mutation": "none — Herdr rejected the prompt before writing to the pane",
                    "reason": str(exc),
                }
            return {
                "status": "draft_inserted_submission_unknown",
                "host_alias": host_alias,
                "terminal_id": terminal_id,
                "action_id": action_id,
                "herdr_error_code": getattr(exc, "code", None),
                "reason": f"submission did not complete cleanly: {exc}",
                "guidance": (
                    "The text may be sitting unsubmitted in the session's composer, "
                    "or may have been submitted — Herdr gives no request ID to tell "
                    "them apart. Inspect the session before doing anything else."
                ),
                "retry": "never — a second attempt could double-submit",
            }
        except Exception as exc:  # noqa: BLE001 — outcome is genuinely unknown.
            # One shape is NOT unknown: the CLI refusing the invocation itself
            # (unknown subcommand -> exit 2 plus its help text). Nothing ran, so
            # nothing was typed. Reporting that as a possible draft sends the
            # operator to inspect a session that was never touched — which is
            # exactly what happened when this wrapper called a verb the
            # installed binary did not have.
            if "herdr agent commands:" in str(exc):
                await _audit(
                    event="submission_rejected",
                    host_alias=host_alias,
                    terminal_id=terminal_id,
                    action_id=action_id,
                    detail=f"cli rejected the invocation: {exc}"[:500],
                )
                await _with_db(
                    lambda conn: herdr_binding.upsert_binding(
                        conn,
                        host_alias=host_alias,
                        terminal_id=terminal_id,
                        completion_state="failed",
                    )
                )
                return {
                    "status": "submission_rejected",
                    "host_alias": host_alias,
                    "terminal_id": terminal_id,
                    "action_id": action_id,
                    "mutation": "none — the Herdr CLI rejected the command before running it",
                    "reason": str(exc)[:300],
                }
            await _audit(
                event="submission_unknown",
                host_alias=host_alias,
                terminal_id=terminal_id,
                action_id=action_id,
                detail=str(exc)[:500],
            )
            await _with_db(
                lambda conn: herdr_binding.upsert_binding(
                    conn,
                    host_alias=host_alias,
                    terminal_id=terminal_id,
                    completion_state="failed",
                )
            )
            return {
                "status": "draft_inserted_submission_unknown",
                "host_alias": host_alias,
                "terminal_id": terminal_id,
                "action_id": action_id,
                "reason": f"submission did not complete cleanly: {exc}",
                "guidance": (
                    "The text may be sitting unsubmitted in the session's composer, "
                    "or may have been submitted — inspect the session before retrying."
                ),
                "retry": "never — a second attempt could double-submit",
            }

        await _audit(
            event="submitted",
            host_alias=host_alias,
            terminal_id=terminal_id,
            action_id=action_id,
        )

        result: Dict[str, Any] = {
            "status": "submitted",
            "host_alias": host_alias,
            "terminal_id": terminal_id,
            "pane_id": session.get("pane_id"),
            "action_id": action_id,
            "completion_state": "pending",
            "completion_signal": COMPLETION_SIGNAL,
            "revision_guard": _revision_guard_state(token_row["revision"]),
            "submission": (
                "Herdr accepted the prompt and scheduled its Enter. Submission is "
                "asynchronous, so this is Herdr's acknowledgement rather than "
                "observed proof the agent has acted on it."
            ),
        }

        if wait_timeout_ms and session.get("pane_id"):
            result.update(
                await _await_completion(
                    client, host_alias, terminal_id, session["pane_id"],
                    action_id, int(wait_timeout_ms),
                )
            )
        return result
    except HerdrError as exc:
        log.warning("herdr_request_action: herdr error on %r: %s", host_alias, exc)
        return {"status": "herdr_error", "host_alias": host_alias, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 — tools must never crash the agent loop.
        log.exception("herdr_request_action: unexpected error for %r", host_alias)
        return {"error": f"unexpected error: {exc}"}


async def _seq_advanced(
    client: HerdrClient,
    pane: str,
    terminal_id: str,
    seq_before: Optional[int],
    tries: int = 6,
) -> Optional[int]:
    """Poll for real agent state movement. Returns the new seq, or None.

    ``state_change_seq`` is Herdr 0.7.5's counter for actual agent state
    changes, as opposed to ``revision``, which only tracks title and metadata.
    Bails out if the pane stops hosting the session we confirmed against, so
    evidence is never read off a different agent.

    Only meaningful for a session that has already taken a turn — during an
    agent's startup window this counter advances on its own as Herdr's
    detection settles, which is why _target_ready refuses cold sessions before
    any of this is reached.
    """
    for _ in range(tries):
        await asyncio.sleep(1.0)
        try:
            raw = await client.get_agent(pane)
        except Exception:  # noqa: BLE001 — treat as no evidence.
            return None
        record = raw.get("agent") if isinstance(raw.get("agent"), dict) else raw
        now = normalize_agent_record(record)
        if now.get("terminal_id") != terminal_id:
            return None  # the pane no longer hosts the session we confirmed against
        seq_now = now.get("state_change_seq")
        if seq_before is None or (seq_now or 0) > seq_before:
            return seq_now
    return None


async def _recover_stalled_submission(
    client: HerdrClient,
    session: Dict[str, Any],
    host_alias: str,
    terminal_id: str,
    action_id: str,
) -> Optional[Dict[str, Any]]:
    """Press Enter on a stalled draft, then require evidence it submitted.

    Returns a success payload only if the session's ``state_change_seq``
    actually advanced — Herdr 0.7.5's counter for real agent state changes, as
    opposed to ``revision``, which only tracks title and metadata. No evidence,
    no success: the caller falls through to draft_inserted_not_submitted.
    """
    pane = session.get("pane_id") or terminal_id
    seq_before = session.get("state_change_seq")
    try:
        await client.submit_enter(pane)
    except Exception as exc:  # noqa: BLE001 — recovery is best effort.
        await _audit(
            event="enter_failed", host_alias=host_alias, terminal_id=terminal_id,
            action_id=action_id, detail=str(exc)[:300],
        )
        return None

    advanced_to = await _seq_advanced(client, pane, terminal_id, seq_before)
    if advanced_to is None:
        return None

    await _audit(
        event="submitted_after_enter", host_alias=host_alias,
        terminal_id=terminal_id, action_id=action_id,
        detail=f"state_change_seq {seq_before} -> {advanced_to}",
    )
    await _with_db(
        lambda conn: herdr_binding.upsert_binding(
            conn, host_alias=host_alias, terminal_id=terminal_id,
            completion_state="pending",
        )
    )
    return {
        "status": "submitted",
        "host_alias": host_alias,
        "terminal_id": terminal_id,
        "pane_id": pane,
        "action_id": action_id,
        "completion_state": "pending",
        "completion_signal": COMPLETION_SIGNAL,
        "submission": (
            "Herdr's own Enter did not land, so Fleet pressed Enter once and "
            "confirmed the session changed state. The prompt text was never "
            "re-sent."
        ),
    }

async def _await_completion(
    client: HerdrClient,
    host_alias: str,
    terminal_id: str,
    pane_id: str,
    action_id: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    """Wait for Herdr's own ``done`` signal — the only completion evidence."""
    try:
        await client.wait_agent_status(pane_id, "done", timeout_ms)
    except Exception as exc:  # noqa: BLE001 — timeout or transport; both unresolved.
        await _audit(
            event="completion_unresolved",
            host_alias=host_alias,
            terminal_id=terminal_id,
            action_id=action_id,
            detail=str(exc)[:300],
        )
        return {
            "completion_state": "pending",
            "completion_note": (
                f"no done signal within {timeout_ms}ms — either still running or the "
                "wait itself failed. Not treated as complete."
            ),
        }
    await _with_db(
        lambda conn: herdr_binding.upsert_binding(
            conn,
            host_alias=host_alias,
            terminal_id=terminal_id,
            completion_state="completed",
        )
    )
    await _audit(
        event="completed",
        host_alias=host_alias,
        terminal_id=terminal_id,
        action_id=action_id,
    )
    return {"completion_state": "completed"}


async def _set_takeover(
    host_alias: str, terminal_id: str, *, claimed: bool
) -> Dict[str, Any]:
    if not isinstance(terminal_id, str) or not terminal_id.strip():
        return {"error": "terminal_id is required"}
    problem = _terminal_id_problem(terminal_id)
    if problem is not None:
        return {
            "status": "invalid_terminal_id",
            "host_alias": host_alias,
            "terminal_id": terminal_id,
            **problem,
        }
    host = await _resolve_host(host_alias)
    if not host["ok"]:
        return host["result"]

    state = "human_takeover" if claimed else "pending"
    await _with_db(
        lambda conn: herdr_binding.upsert_binding(
            conn,
            host_alias=host_alias,
            terminal_id=terminal_id,
            completion_state=state,
        )
    )
    await _audit(
        event="human_takeover_claimed" if claimed else "human_takeover_released",
        host_alias=host_alias,
        terminal_id=terminal_id,
    )
    binding = await _with_db(
        lambda conn: herdr_binding.get_binding(conn, host_alias, terminal_id)
    )

    return {
        "status": "human_takeover" if claimed else "automation_permitted",
        "host_alias": host_alias,
        "terminal_id": terminal_id,
        "binding": binding,
        "note": (
            "Fleet-side pause only. Herdr's authority verbs are deliberately not used: "
            "its --source is a fixed allowlist and ('herdr:hermes','hermes') is "
            "session-identity-only, so Herdr discards authority reports from Hermes. "
            "The Herdr session itself is untouched and keeps running."
        ),
    }


async def herdr_claim_human_takeover_handler(
    params: Any = None,
    host_alias: str = "",
    terminal_id: str = "",
    _injected: Any = None,
    **_kw: Any,
) -> Dict[str, Any]:
    """Pause Fleet automation for one session without disturbing it."""
    if isinstance(params, dict):
        host_alias = params.get("host_alias", "") or host_alias
        terminal_id = params.get("terminal_id", "") or terminal_id
    try:
        return await _set_takeover(host_alias, terminal_id, claimed=True)
    except Exception as exc:  # noqa: BLE001 — tools must never crash the agent loop.
        log.exception("herdr_claim_human_takeover failed for %r", host_alias)
        return {"error": f"unexpected error: {exc}"}


async def herdr_release_human_takeover_handler(
    params: Any = None,
    host_alias: str = "",
    terminal_id: str = "",
    _injected: Any = None,
    **_kw: Any,
) -> Dict[str, Any]:
    """Hand one session back to Fleet automation."""
    if isinstance(params, dict):
        host_alias = params.get("host_alias", "") or host_alias
        terminal_id = params.get("terminal_id", "") or terminal_id
    try:
        return await _set_takeover(host_alias, terminal_id, claimed=False)
    except Exception as exc:  # noqa: BLE001 — tools must never crash the agent loop.
        log.exception("herdr_release_human_takeover failed for %r", host_alias)
        return {"error": f"unexpected error: {exc}"}
