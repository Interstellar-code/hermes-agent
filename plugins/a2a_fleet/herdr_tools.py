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

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .herdr_capability import check_herdr_capability
from .herdr_client import HerdrClient, HerdrError, HerdrNotFound, normalize_agent_record

log = logging.getLogger("a2a_fleet.herdr_tools")


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
