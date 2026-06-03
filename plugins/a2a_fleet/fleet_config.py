"""Fleet config loader for the a2a_fleet plugin.

Reads a standalone ``fleet.yaml`` from the active Hermes home
(``get_hermes_home() / "fleet.yaml"``).  Hermes sets ``HERMES_HOME`` to the
active profile directory before plugin import, so no extra ``profiles/<name>``
path segment is appended here.

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

import logging
import os
from pathlib import Path

from hermes_constants import get_hermes_home
from typing import Any, Dict
from urllib.parse import urlparse

import yaml

from .managed_peers import (
    stable_token_env_name,
    supports_managed_mode,
    token_filename_for,
)


log = logging.getLogger("a2a_fleet.fleet_config")

SUPPORTED_HANDLERS = {"echo", "llm", "agent"}

_ALLOWED_SCHEMES = {"http", "https"}

# Managed receiver peers need a generous turn timeout. Below this we warn
# (not error) so a short global Route B default doesn't silently look like a
# failure for these peers.
_MANAGED_PEER_MIN_TIMEOUT_S = 300


def _validate_peer_url(url: str | None, field: str, peer_name: str, path: Path) -> None:
    """Reject peer URLs that are missing, have no real host, or use non-http(s) schemes."""
    if not url:
        raise FleetConfigError(
            f"{path}: fleet.agents.{peer_name}.{field} is required"
        )
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise FleetConfigError(
            f"{path}: fleet.agents.{peer_name}.{field} scheme must be http or https, "
            f"got {parsed.scheme!r} in {url!r}"
        )
    if not parsed.hostname:
        raise FleetConfigError(
            f"{path}: fleet.agents.{peer_name}.{field} must have a valid host in {url!r}"
        )


class FleetConfigError(ValueError):
    """Raised when fleet.yaml is missing, malformed, or unsupported."""


def _legacy_profile_name(profile: str | None = None) -> str:
    if profile:
        return profile.strip()
    env_profile = os.environ.get("HERMES_PROFILE")
    if env_profile:
        return env_profile.strip()
    marker = get_hermes_home() / "active_profile"
    if marker.is_file():
        name = marker.read_text(encoding="utf-8").strip()
        if name:
            return name
    return "default"


def _fleet_yaml_path(profile: str | None = None) -> Path:
    """Return the fleet config path for the active Hermes profile.

    Hermes profile selection is already reflected in ``HERMES_HOME`` by
    ``hermes_cli.main._apply_profile_override()``.  Prefer ``fleet.yaml``
    directly under the active Hermes home.  For compatibility with early
    a2a_fleet checkouts, fall back to ``profiles/<name>/fleet.yaml`` only when
    that legacy file exists below the current Hermes home.
    """
    home = get_hermes_home()
    primary = home / "fleet.yaml"
    if primary.is_file():
        return primary
    legacy = home / "profiles" / _legacy_profile_name(profile) / "fleet.yaml"
    if legacy.is_file():
        return legacy
    return primary


def _resolve_token(token_env: str | None) -> str | None:
    if not token_env:
        return None
    return os.environ.get(token_env)


def _resolve_managed_token(
    token_env: str | None, mode: object, repo_path: object
) -> str | None:
    """Resolve a MANAGED peer's inbound bearer token.

    The persisted ``<repo>/.hermes/<token_filename>`` is AUTHORITATIVE: it is
    exactly the token the currently-running receiver was launched requiring, and
    every deploy writes it last. ``os.environ[token_env]`` is only an in-process
    cache — it is correct in the process that ran the deploy, but goes STALE in
    any other process across an out-of-process redeploy. Resolving the stale env
    value sends the wrong bearer -> HTTP 401 (P0-3); resolving an UNSET env value
    sends no bearer -> 401 (issue #104). So prefer the file, and fall back to the
    env var only when the file is absent/unreadable (e.g. a managed peer whose
    receiver has not been deployed on this host yet).
    """
    if supports_managed_mode(mode) and repo_path:
        fname = token_filename_for(str(mode))
        if fname:
            try:
                raw = (Path(str(repo_path)) / ".hermes" / fname).read_text(encoding="utf-8").strip()
            except OSError:
                raw = ""
            if raw:
                return raw
    return _resolve_token(token_env)


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
            f"response_handler {handler!r} not supported, "
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
        "name": self_block.get("name") or _legacy_profile_name(profile),
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
    has_managed_receiver_peer = False
    for name, entry in agents_in.items():
        if not isinstance(entry, dict):
            raise FleetConfigError(
                f"{path}: fleet.agents.{name} must be a mapping, got {type(entry).__name__}"
            )
        peer_url = entry.get("url")
        peer_card_url = entry.get("agent_card_url")
        _validate_peer_url(peer_url, "url", name, path)
        if peer_card_url:
            _validate_peer_url(peer_card_url, "agent_card_url", name, path)
        # v0.3+ repo-aware peer fields (additive, all OPTIONAL). A plain url/token
        # peer omits these and gets inert defaults below, so existing fleets are
        # unaffected. ``managed`` + supported ``mode`` + ``repo_path`` together
        # mark a Hermes-managed receiver that boot-reconcile owns.
        repo_path = entry.get("repo_path")
        # ``managed`` must be a real Python bool. A truthy string such as the YAML
        # value "false" would otherwise bool()-coerce to True and silently flip a
        # peer into managed mode (#5). Absent -> default False.
        managed_raw = entry.get("managed", False)
        if not isinstance(managed_raw, bool):
            raise FleetConfigError(
                f"{path}: fleet.agents.{name}.managed must be a boolean (true/false), "
                f"got {type(managed_raw).__name__} {managed_raw!r}"
            )
        token_env = entry.get("token_env")
        mode = entry.get("mode")

        # Single source of truth for the token env-var NAME of a managed
        # receiver: it MUST equal the mode-specific stable name so boot-reconcile
        # and fleet_send resolve the same var.
        if managed_raw and supports_managed_mode(mode) and repo_path:
            has_managed_receiver_peer = True
            stable = stable_token_env_name(str(mode), str(repo_path))
            if token_env != stable:
                raise FleetConfigError(
                    f"{path}: managed {mode} peer {name}: token_env must be "
                    f"{stable} for repo {repo_path} (got {token_env!r})"
                )

        agents_out[name] = {
            "url": peer_url,
            "agent_card_url": peer_card_url,
            # Managed peers fall back to the persisted .token (issue #104); plain
            # peers resolve from the env var only.
            "token": (
                _resolve_managed_token(token_env, mode, repo_path)
                if managed_raw
                else _resolve_token(token_env)
            ),
            "token_env": token_env,
            "description": entry.get("description", ""),
            "repo_path": str(repo_path) if repo_path else None,
            "managed": managed_raw,
            "mode": mode,
        }

    # Optional llm block — system_prompt / system_prompt_file, max_tokens, temperature.
    # Provider/api_key are intentionally NOT read here; those come from the active
    # profile via resolve_provider_client("auto").
    llm_raw = fleet.get("llm") or {}

    # Security: validate system_prompt_file is within get_hermes_home() to prevent
    # path-traversal attacks (issue #84).
    sp_file_raw = llm_raw.get("system_prompt_file")
    validated_sp_file: str | None = None
    if sp_file_raw is not None:
        sp_file_str = os.path.expanduser(str(sp_file_raw))
        sp_path = Path(sp_file_str)
        if not sp_path.is_absolute():
            sp_path = get_hermes_home() / sp_path
        resolved = sp_path.resolve()
        root = get_hermes_home().resolve()
        if not resolved.is_relative_to(root):
            raise FleetConfigError(
                f"llm.system_prompt_file must be within {root}, got {resolved}"
            )
        validated_sp_file = str(resolved)

    llm_block: Dict[str, Any] = {
        "system_prompt": llm_raw.get("system_prompt"),
        "system_prompt_file": validated_sp_file,
        "max_tokens": int(llm_raw.get("max_tokens", 2048)),
        "temperature": float(llm_raw.get("temperature", 0.7)),
    }

    # Optional agent block — timeout_s for synchronous Route B dispatch.
    agent_raw = fleet.get("agent") or {}
    agent_block: Dict[str, Any] = {
        "timeout_s": int(agent_raw.get("timeout_s", 120)),
    }

    # Managed executor peers need a generous turn timeout. Do NOT change the
    # global Route B default (120); just WARN if it is under the recommended floor
    # so these longer turns are not mistaken for failures.
    if has_managed_receiver_peer and agent_block["timeout_s"] < _MANAGED_PEER_MIN_TIMEOUT_S:
        log.warning(
            "a2a_fleet: fleet.agent.timeout_s=%s is below the recommended %s for "
            "managed claude_code/opencode peers; a short timeout will look like a "
            "failure while the executor is still working.",
            agent_block["timeout_s"], _MANAGED_PEER_MIN_TIMEOUT_S,
        )

    return {
        "enabled": bool(fleet.get("enabled", True)),
        "response_handler": handler,
        "self": out_self,
        "agents": agents_out,
        "llm": llm_block,
        "agent": agent_block,
    }


def get_agent(name: str, profile: str | None = None) -> Dict[str, Any]:
    """Lookup a single peer by name. Raises KeyError if not configured."""
    cfg = load_fleet(profile)
    if name not in cfg["agents"]:
        raise KeyError(f"agent {name!r} not in fleet.yaml; configured: {list(cfg['agents'])}")
    return cfg["agents"][name]
