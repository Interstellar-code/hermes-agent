"""Agent-facing read-only Herdr tools for the a2a_fleet plugin.

Phase 1 ships three tools — ``herdr_status``, ``herdr_list_sessions``, and
``herdr_inspect_session`` — all strictly read-only. No tool here mutates a live
pane; ``agent send`` / ``pane send-keys`` / ``pane run`` are deliberately not
wrapped (Phase 2, gated on confirmation tokens that do not exist yet).

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
from .herdr_client import HerdrClient, HerdrError, HerdrNotFound, normalize_agent_record

log = logging.getLogger("a2a_fleet.herdr_tools")

# The completion signal Fleet accepts. Named here so the preview payload can
# state it up front: the operator confirming an action is told exactly what
# will be treated as "done", and it is never silence or an idle prompt.
COMPLETION_SIGNAL = "herdr wait agent-status --status done"


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

        try:
            raw = await client.get_agent(terminal_id)
        except HerdrNotFound:
            return {
                "status": "not_found",
                "host_alias": host_alias,
                "terminal_id": terminal_id,
                "reason": f"no Herdr session with terminal_id {terminal_id!r}",
            }

        record = raw.get("agent") if isinstance(raw.get("agent"), dict) else raw
        normalized = normalize_agent_record(record)

        # EXACT IDENTIFIER ENFORCEMENT. Herdr's `agent get` accepts terminal ids,
        # unique agent names, AND non-unique kind labels ("claude", "opencode"),
        # resolving a label to whichever pane happens to match. Verified live:
        # passing "claude" returned a session successfully. That is the ambiguous
        # selection this tool exists to prevent, so require the resolved record
        # to be the exact session asked for and reject anything else — a caller
        # that wants to search should use herdr_list_sessions.
        resolved_id = normalized.get("terminal_id")
        if resolved_id != terminal_id:
            return {
                "status": "ambiguous_identifier",
                "host_alias": host_alias,
                "requested": terminal_id,
                "resolved_terminal_id": resolved_id,
                "reason": (
                    f"{terminal_id!r} is not a terminal_id — Herdr resolved it to session "
                    f"{resolved_id!r}. Agent kind labels are not unique across panes and are "
                    f"rejected as identifiers. Use herdr_list_sessions to get the exact "
                    f"terminal_id."
                ),
            }

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
            "agent_kind": session.get("agent"),
            "workspace": session.get("cwd"),
            "action": action,
            "summary": summary,
            "revision_at_preview": session.get("revision"),
            "completion_signal": COMPLETION_SIGNAL,
            "confirmation_token": issued["token"],
            "action_id": issued["action_id"],
            "expires_in_seconds": herdr_binding.TOKEN_TTL_SECONDS,
            "note": (
                "Single use. herdr_request_action refuses if the session's revision "
                "has moved since this preview."
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

        # Optimistic concurrency. The token is already spent, so a moved
        # revision fails closed rather than re-arming. `revision` counts pane
        # *output* changes (protocol 16 carries it on pane_output_changed
        # only), which is exactly "something happened here since you looked".
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
                agent_kind=session.get("agent"),
                workspace_canonical_path=session.get("cwd"),
                revision_at_preview=token_row["revision"],
                action_id=action_id,
                transport_kind=host_cfg.get("transport"),
                completion_state="pending",
            )
        )
        # Audit the intent BEFORE the send. If this process dies mid-call the
        # trail still shows an action whose outcome is unknown — which is the
        # honest state. An audit written only on success would leave a
        # delivered action with no record of it at all.
        await _audit(
            event="send_attempted",
            host_alias=host_alias,
            terminal_id=terminal_id,
            action_id=action_id,
            detail=token_row["action"][:500],
        )

        client = _client_for(host_cfg)
        try:
            await client.send_agent_text(terminal_id, token_row["action"])
        except Exception as exc:  # noqa: BLE001 — outcome is genuinely unknown.
            await _audit(
                event="send_outcome_unknown",
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
                "status": "outcome_unknown",
                "host_alias": host_alias,
                "terminal_id": terminal_id,
                "action_id": action_id,
                "reason": f"send did not complete cleanly: {exc}",
                "retry": "never — this action has no request ID and may have landed",
            }

        await _audit(
            event="sent",
            host_alias=host_alias,
            terminal_id=terminal_id,
            action_id=action_id,
        )

        result: Dict[str, Any] = {
            "status": "sent",
            "host_alias": host_alias,
            "terminal_id": terminal_id,
            "pane_id": session.get("pane_id"),
            "action_id": action_id,
            "completion_state": "pending",
            "completion_signal": COMPLETION_SIGNAL,
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
