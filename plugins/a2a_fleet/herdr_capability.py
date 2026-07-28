"""Herdr dependency probe: a structured, never-raising capability check.

Gates Fleet's Herdr tools. Capability is decided by a live probe (``herdr
status`` / ``herdr api schema``), never by checking whether the
``herdr-agent-state`` Hermes integration asset is installed or loaded — that
asset only reports Hermes state to Herdr and says nothing about Fleet's
ability to drive Herdr, so gating on it would produce false negatives.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from .herdr_client import HerdrClient, HerdrError

# (label, help subcommand args, substring expected in that --help text)
# Herdr exposes no capability-enumeration endpoint, so verb presence is
# probed against --help output rather than name-matched against a list.
_REQUIRED_VERBS = (
    ("agent list", ("agent", "--help"), "agent list"),
    ("agent get", ("agent", "--help"), "agent get"),
    ("agent read", ("agent", "--help"), "agent read"),
    ("wait agent-status", ("wait", "--help"), "wait agent-status"),
)


def _resolve_socket_path() -> str:
    # Herdr's own session-scoped env var, then its documented default.
    return os.environ.get("HERDR_SOCKET_PATH") or str(
        Path.home() / ".config" / "herdr" / "herdr.sock"
    )


async def _probe_required_verbs(client: HerdrClient) -> List[str]:
    missing: List[str] = []
    help_cache: Dict[tuple, str] = {}
    for label, help_args, needle in _REQUIRED_VERBS:
        if help_args not in help_cache:
            try:
                help_cache[help_args] = await client.help_text(*help_args)
            except HerdrError:
                help_cache[help_args] = ""
        if needle not in help_cache[help_args]:
            missing.append(label)
    return missing


async def check_herdr_capability(
    host_alias: str,
    herdr_cfg: Optional[Dict[str, Any]] = None,
    *,
    binary: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """Probe Herdr's live capability for ``host_alias``. Never raises.

    ``herdr_cfg`` is the plain ``fleet.herdr`` dict (``{"hosts": {...}}``);
    callers own config loading, this function only reads ``hosts.<alias>``.
    """
    herdr_cfg = herdr_cfg or {}
    host_cfg = (herdr_cfg.get("hosts") or {}).get(host_alias, {})
    transport = host_cfg.get("transport", "local_socket")
    ssh_target = host_cfg.get("ssh_target") if transport == "ssh_bridge" else None

    resolved_binary = binary or shutil.which("herdr")
    if not resolved_binary:
        return {
            "status": "herdr_missing",
            "reason": "herdr binary not found on PATH",
            "install_hint": "install herdr and ensure it is on PATH (see herdr's own install docs)",
            "host_alias": host_alias,
        }

    client = HerdrClient(binary=resolved_binary, ssh_target=ssh_target, timeout=timeout)

    try:
        status_result = await client.status()
    except HerdrError as exc:
        return {
            "status": "herdr_unreachable",
            "reason": str(exc),
            "host_alias": host_alias,
            "socket": _resolve_socket_path(),
        }
    except Exception as exc:  # pragma: no cover - defensive, this must never raise
        return {
            "status": "herdr_unreachable",
            "reason": f"unexpected error probing herdr: {exc}",
            "host_alias": host_alias,
        }

    server = status_result.get("server") or {}
    if server.get("status") != "running":
        return {
            "status": "herdr_unreachable",
            "reason": f"herdr server not running (status={server.get('status')!r})",
            "host_alias": host_alias,
            "socket": server.get("socket") or _resolve_socket_path(),
        }

    try:
        schema_result = await client.schema()
    except HerdrError as exc:
        return {
            "status": "herdr_unreachable",
            "reason": f"herdr api schema failed: {exc}",
            "host_alias": host_alias,
            "socket": server.get("socket") or _resolve_socket_path(),
        }

    actual_protocol = schema_result.get("protocol")
    if actual_protocol != HerdrClient.PROTOCOL_VERSION:
        return {
            "status": "herdr_protocol_mismatch",
            "expected": HerdrClient.PROTOCOL_VERSION,
            "actual": actual_protocol,
            "host_alias": host_alias,
        }

    missing_verbs = await _probe_required_verbs(client)
    if missing_verbs:
        return {
            "status": "herdr_verbs_missing",
            "missing": missing_verbs,
            "host_alias": host_alias,
        }

    return {
        "status": "ok",
        "version": (status_result.get("client") or {}).get("version"),
        "protocol": actual_protocol,
        "socket": server.get("socket"),
        "transport": "ssh_bridge" if ssh_target else "local_socket",
        "host_alias": host_alias,
        # informational only, never a gate (see module docstring)
        "herdr_env_pane_detected": bool(os.environ.get("HERDR_ENV")),
    }
