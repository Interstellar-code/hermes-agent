"""Comment-preserving fleet.yaml scaffolding + peer auto-wiring for a2a_fleet.

Closes two onboarding gaps that made the plugin painful to bring up:

1. ``ensure_example_fleet_yaml()`` — on first plugin enable, write a commented
   example ``fleet.yaml`` to the active profile home if absent. Before this, a
   fresh profile that enabled a2a_fleet had no fleet.yaml, ``load_fleet()`` raised
   ``FleetConfigError``, and ``register()`` went *silently idle* with only a log
   line. Now the node comes up with a documented, editable scaffold.

2. ``upsert_managed_peer()`` + back-compat wrappers — after a managed receiver
   deploy stands up, write (or refresh) its peer entry in fleet.yaml
   *surgically*, preserving the user's comments and formatting via ruamel
   round-trip. Before this, the operator had to hand-edit fleet.yaml to add
   ``url`` + ``token_env`` — the omission caused a 401 on ``fleet_send``.
   Managed Claude Code and OpenCode peers also let boot-reconcile re-provision
   the inbound token across a gateway restart.

ruamel.yaml (a declared dependency, see pyproject.toml) is used for the surgical
upsert; the scaffold is a static commented string so its guidance reads cleanly.
"""
from __future__ import annotations

import io
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ruamel.yaml import YAML
from ruamel.yaml.constructor import ConstructorError

# Reuse the single source of truth for path resolution so scaffold + upsert + load
# all agree on WHERE fleet.yaml lives for the active profile.
from .fleet_config import _fleet_yaml_path, _legacy_profile_name
from .managed_peers import (
    canonicalize_managed_repo_path,
    managed_peer_default_name,
    managed_peer_description,
)

log = logging.getLogger("a2a_fleet.fleet_yaml_io")


def fleet_yaml_path(profile: str | None = None) -> Path:
    """Public accessor for the active profile's fleet.yaml path."""
    return _fleet_yaml_path(profile)


# --------------------------------------------------------------------------- #
# 1. First-enable scaffold
# --------------------------------------------------------------------------- #

def _example_fleet_yaml(self_name: str) -> str:
    """Return a commented example fleet.yaml body (loads cleanly via load_fleet).

    ``enabled: true`` + ``response_handler: agent`` so a fresh profile that turns
    the plugin on actually participates (the inbound node binds on the loopback
    port below). The peers map is empty — ``deploy_cc_receiver`` auto-wires a
    managed Claude Code peer here later; the commented block shows the shape.
    """
    return f"""\
# a2a_fleet config — Agent-to-Agent fleet membership.
# Scaffolded by the a2a_fleet plugin on first enable. Edit freely: the plugin
# preserves your comments when deploy_cc_receiver auto-wires a peer below.
# Full docs: plugins/a2a_fleet/README.md
fleet:
  # Master switch. Set false to keep the plugin installed but stay out of any fleet.
  enabled: true

  # How THIS node answers inbound A2A messages:
  #   echo  — debug ping/pong (no model)
  #   llm   — stateless model reply via the active profile's provider
  #   agent — dispatch into the REAL Hermes agent (tools/memory/SOUL) [Route B]
  response_handler: agent

  # Inbound A2A server. Pick a free TCP port unique to this profile.
  server:
    bind_host: 127.0.0.1
    bind_port: 9219
    auth_required: false
    # token_env: {self_name.upper()}_A2A_TOKEN  # require Bearer inbound when auth_required: true

  self:
    name: {self_name}

  # Route B turn budget. Managed Claude Code peers need >= 300s — a tool-using
  # `claude -p` turn runs 30s–5min and a short timeout looks like a failure.
  agent:
    timeout_s: 300

  # Fleet peers you can fleet_send() to. A managed Claude Code executor receiver
  # is AUTO-WIRED here by deploy_cc_receiver — you do NOT hand-edit it:
  #
  # agents:
  #   claude-code:
  #     url: http://127.0.0.1:9300
  #     token_env: A2A_CC_TOKEN_<REPO>
  #     managed: true
  #     mode: claude_code
  #     repo_path: /abs/path/to/repo
  #     description: "Claude Code executor receiver"
  agents: {{}}
"""


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (unique temp in same dir + os.replace).

    A UNIQUE temp name (mkstemp) avoids two near-simultaneous writers colliding on
    a shared ``fleet.yaml.tmp``. Note: this makes the *write* atomic, but the
    read-modify-write of upsert_cc_peer is NOT locked — concurrent deploys against
    the same profile's fleet.yaml are not safe (last writer wins). Hermes serializes
    deploys, so this is acceptable; revisit with an flock if that changes.
    """
    import tempfile  # noqa: PLC0415 — stdlib, lazy keeps import surface small.

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)  # atomic on POSIX
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def ensure_example_fleet_yaml(profile: str | None = None) -> Tuple[Path, bool]:
    """Write a commented example fleet.yaml to the profile home if absent.

    Idempotent: returns ``(path, False)`` and leaves the file untouched when one
    already exists. Returns ``(path, True)`` when it scaffolds a fresh one. Never
    raises on a write failure — logs a warning and returns ``(path, False)`` so a
    read-only home never breaks plugin load.
    """
    path = _fleet_yaml_path(profile)
    if path.exists():
        return path, False
    self_name = _legacy_profile_name(profile)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, _example_fleet_yaml(self_name))
    except OSError as exc:
        log.warning("a2a_fleet: could not scaffold example fleet.yaml at %s: %s", path, exc)
        return path, False
    log.info("a2a_fleet: scaffolded example fleet.yaml at %s (edit it to join a fleet)", path)
    return path, True


# --------------------------------------------------------------------------- #
# 2. Comment-preserving managed-peer upsert (post deploy_cc_receiver)
# --------------------------------------------------------------------------- #

def _yaml() -> YAML:
    y = YAML()  # round-trip mode (preserves comments + formatting)
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)

    def _reject_python_tag(constructor, tag_suffix, node):
        raise ConstructorError(
            None, None,
            f"unsafe python tag '!!python/{tag_suffix}' is forbidden in fleet.yaml",
            node.start_mark,
        )
    y.constructor.add_multi_constructor("tag:yaml.org,2002:python/", _reject_python_tag)
    return y


def _repo_slug(repo_path: str) -> str:
    base = Path(repo_path).name or "repo"
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return slug or "repo"


def upsert_managed_peer(
    *,
    repo_path: str,
    url: str,
    token_env: str = "",
    name: str,
    mode: str,
    description: str = "",
    profile: str | None = None,
) -> Dict[str, Any]:
    """Insert or refresh a managed receiver peer in fleet.yaml, surgically.

    Comment- and format-preserving (ruamel round-trip). When ``token_env`` is set
    the peer is written as a managed peer (``managed: true`` + ``mode`` +
    canonical ``repo_path``) so:
      * fleet_send resolves the bearer from ``os.environ[token_env]``, and
      * boot-reconcile can re-provision the token + receiver across restart.
    A ``token_env``-less (``no_auth``) deploy gets a plain ``url`` peer (nothing
    to re-provision).

    Peer keyed by ``name``. If that name is already taken by a different repo, a
    repo-suffixed name is used so distinct receivers do not clobber each other.

    Returns ``{"action": "created"|"updated"|"unchanged", "name": ..., "path": ...}``.
    Never raises: returns ``{"error": "..."}`` on any IO/parse failure so deploy
    success is never masked by a config-write hiccup.
    """
    path = _fleet_yaml_path(profile)
    yaml = _yaml()

    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.load(fh)
        else:
            data = None
        if data is None:
            ensure_example_fleet_yaml(profile)
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.load(fh)
    except Exception as exc:  # noqa: BLE001 — config write must never mask deploy
        return {"error": f"could not read fleet.yaml at {path}: {exc}"}

    fleet = data.get("fleet")
    if not isinstance(fleet, dict):
        return {"error": f"{path}: missing top-level 'fleet:' mapping"}

    agents = fleet.get("agents")
    if not isinstance(agents, dict):
        from ruamel.yaml.comments import CommentedMap  # noqa: PLC0415
        agents = CommentedMap()
        fleet["agents"] = agents

    repo_canon_path, _ = canonicalize_managed_repo_path(repo_path)
    canon = str(repo_canon_path or Path(str(repo_path)))
    chosen = name
    existing = agents.get(chosen)
    if isinstance(existing, dict):
        ex_repo = existing.get("repo_path")
        if ex_repo and str(ex_repo) != canon:
            chosen = f"{name}-{_repo_slug(canon)}"

    from ruamel.yaml.comments import CommentedMap  # noqa: PLC0415
    peer = agents.get(chosen)
    is_new = not isinstance(peer, dict)
    if is_new:
        peer = CommentedMap()

    before = dict(peer) if isinstance(peer, dict) else {}

    peer["url"] = url
    peer["agent_card_url"] = f"{url.rstrip('/')}/.well-known/agent-card.json"
    if token_env:
        peer["token_env"] = token_env
        peer["managed"] = True
        peer["mode"] = mode
        peer["repo_path"] = canon
    else:
        for k in ("token_env", "managed", "mode", "repo_path"):
            peer.pop(k, None)
    peer["description"] = description or managed_peer_description(mode, canon)

    agents[chosen] = peer

    after = dict(peer)
    if not is_new and before == after:
        return {"action": "unchanged", "name": chosen, "path": str(path)}

    try:
        buf = io.StringIO()
        yaml.dump(data, buf)
        _atomic_write_text(path, buf.getvalue())
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not write fleet.yaml at {path}: {exc}"}

    return {
        "action": "created" if is_new else "updated",
        "name": chosen,
        "path": str(path),
    }


def upsert_cc_peer(
    *,
    repo_path: str,
    url: str,
    token_env: str = "",
    name: str = "claude-code",
    description: str = "",
    profile: str | None = None,
) -> Dict[str, Any]:
    """Back-compat Claude Code wrapper around ``upsert_managed_peer()``."""
    return upsert_managed_peer(
        repo_path=repo_path,
        url=url,
        token_env=token_env,
        name=name or managed_peer_default_name("claude_code"),
        mode="claude_code",
        description=description,
        profile=profile,
    )


def upsert_oc_peer(
    *,
    repo_path: str,
    url: str,
    token_env: str = "",
    name: str = "opencode",
    description: str = "",
    profile: str | None = None,
) -> Dict[str, Any]:
    """OpenCode wrapper around ``upsert_managed_peer()``."""
    return upsert_managed_peer(
        repo_path=repo_path,
        url=url,
        token_env=token_env,
        name=name or managed_peer_default_name("opencode"),
        mode="opencode",
        description=description,
        profile=profile,
    )


def upsert_codex_peer(
    *,
    repo_path: str,
    url: str,
    token_env: str = "",
    name: str = "codex",
    description: str = "",
    profile: str | None = None,
) -> Dict[str, Any]:
    """Codex CLI wrapper around ``upsert_managed_peer()``."""
    return upsert_managed_peer(
        repo_path=repo_path,
        url=url,
        token_env=token_env,
        name=name or managed_peer_default_name("codex"),
        mode="codex",
        description=description,
        profile=profile,
    )


def upsert_agy_peer(
    *,
    repo_path: str,
    url: str,
    token_env: str = "",
    name: str = "agy",
    description: str = "",
    profile: str | None = None,
) -> Dict[str, Any]:
    """Google Antigravity CLI wrapper around ``upsert_managed_peer()``."""
    return upsert_managed_peer(
        repo_path=repo_path,
        url=url,
        token_env=token_env,
        name=name or managed_peer_default_name("agy"),
        mode="agy",
        description=description,
        profile=profile,
    )
