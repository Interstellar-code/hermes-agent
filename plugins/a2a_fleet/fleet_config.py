"""Fleet config loader for the a2a_fleet plugin.

Reads a standalone ``fleet.yaml`` from
``~/.hermes/profiles/<profile>/fleet.yaml`` (not embedded in ``config.yaml``).
The active profile is resolved from the ``HERMES_PROFILE`` env var, else
from ``$HERMES_HOME/active_profile``.

``self.url`` is **never** cached at import time. The plugin api module is
imported by ``_mount_plugin_api_routes()`` before the gateway binds, so any
host/port resolved here would be unreliable. The Agent Card route resolves
its own URL per-request via ``request.base_url``.

Acceptance for v0.1 (Step 1 of plan):
- ``load_fleet()`` returns ``{self: {name, token}, agents: {name → {url, agent_card_url, token, description}}}``
- Setting ``response_handler: llm`` (or anything other than ``echo``) raises
  ``ValueError`` immediately on load.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml


SUPPORTED_HANDLERS = {"echo"}


class FleetConfigError(ValueError):
    """Raised when fleet.yaml is missing, malformed, or unsupported."""


def _hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home).expanduser()
    return Path.home() / ".hermes"


def _active_profile() -> str:
    profile = os.environ.get("HERMES_PROFILE")
    if profile:
        return profile.strip()
    marker = _hermes_home() / "active_profile"
    if marker.is_file():
        name = marker.read_text(encoding="utf-8").strip()
        if name:
            return name
    return "default"


def _fleet_yaml_path(profile: str | None = None) -> Path:
    name = profile or _active_profile()
    return _hermes_home() / "profiles" / name / "fleet.yaml"


def _resolve_token(token_env: str | None) -> str | None:
    if not token_env:
        return None
    return os.environ.get(token_env)


def load_fleet(profile: str | None = None) -> Dict[str, Any]:
    """Read fleet.yaml for the active (or named) profile.

    Raises FleetConfigError when the file is missing, when ``fleet`` is not
    a mapping, or when ``response_handler`` is unsupported in v0.1.
    """
    path = _fleet_yaml_path(profile)
    if not path.is_file():
        raise FleetConfigError(f"fleet.yaml not found at {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    fleet = raw.get("fleet")
    if not isinstance(fleet, dict):
        raise FleetConfigError(
            f"{path}: expected top-level 'fleet:' mapping, got {type(fleet).__name__}"
        )

    handler = (fleet.get("response_handler") or "echo").strip()
    if handler not in SUPPORTED_HANDLERS:
        raise FleetConfigError(
            f"response_handler {handler!r} not supported in v0.1, "
            f"only {sorted(SUPPORTED_HANDLERS)} are implemented."
        )

    self_block = fleet.get("self") or {}
    server_block = fleet.get("server") or {}

    self_token = _resolve_token(server_block.get("token_env"))

    bind_host = server_block.get("bind_host", "127.0.0.1") or "127.0.0.1"
    bind_port = server_block.get("bind_port")
    if bind_port is None:
        raise FleetConfigError(
            f"{path}: fleet.server.bind_port is required (no default — pick a free TCP port for this profile)."
        )
    try:
        bind_port = int(bind_port)
    except (TypeError, ValueError) as exc:
        raise FleetConfigError(
            f"{path}: fleet.server.bind_port must be an integer, got {bind_port!r}"
        ) from exc

    out_self: Dict[str, Any] = {
        "name": self_block.get("name") or _active_profile(),
        "token": self_token,
        "auth_required": bool(server_block.get("auth_required", True)),
        "token_env": server_block.get("token_env"),
        "bind_host": bind_host,
        "bind_port": bind_port,
    }

    agents_in = fleet.get("agents") or {}
    if not isinstance(agents_in, dict):
        raise FleetConfigError(
            f"{path}: expected 'fleet.agents:' mapping, got {type(agents_in).__name__}"
        )

    agents_out: Dict[str, Dict[str, Any]] = {}
    for name, entry in agents_in.items():
        if not isinstance(entry, dict):
            raise FleetConfigError(
                f"{path}: fleet.agents.{name} must be a mapping, got {type(entry).__name__}"
            )
        agents_out[name] = {
            "url": entry.get("url"),
            "agent_card_url": entry.get("agent_card_url"),
            "token": _resolve_token(entry.get("token_env")),
            "token_env": entry.get("token_env"),
            "description": entry.get("description", ""),
        }

    return {
        "enabled": bool(fleet.get("enabled", True)),
        "response_handler": handler,
        "self": out_self,
        "agents": agents_out,
    }


def get_agent(name: str, profile: str | None = None) -> Dict[str, Any]:
    """Lookup a single peer by name. Raises KeyError if not configured."""
    cfg = load_fleet(profile)
    if name not in cfg["agents"]:
        raise KeyError(f"agent {name!r} not in fleet.yaml; configured: {list(cfg['agents'])}")
    return cfg["agents"][name]
