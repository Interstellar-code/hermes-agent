"""Shared managed-peer helpers for a2a_fleet config/deploy plumbing.

This module is intentionally small and dependency-light so both config loading
and future boot-reconcile paths can share the same managed-peer mode contracts
without pulling in receiver runtime code at import time.
"""
from __future__ import annotations

import hashlib
import importlib
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple

SUPPORTED_MANAGED_MODES = frozenset({"claude_code", "opencode", "codex", "agy"})

_MODE_SPECS: Dict[str, Dict[str, str]] = {
    "claude_code": {
        "default_name": "claude-code",
        "description": "Claude Code executor receiver",
        "token_prefix": "A2A_CC_TOKEN_",
        "deploy_module": "cc_deploy",
        "transcript_filename": "a2a-transcript.jsonl",
    },
    "opencode": {
        "default_name": "opencode",
        "description": "OpenCode executor receiver",
        "token_prefix": "A2A_OC_TOKEN_",
        "deploy_module": "oc_deploy",
        "transcript_filename": "a2a-oc-transcript.jsonl",
    },
    "codex": {
        "default_name": "codex",
        "description": "Codex CLI executor receiver",
        "token_prefix": "A2A_CODEX_TOKEN_",
        "deploy_module": "codex_deploy",
        "transcript_filename": "a2a-codex-transcript.jsonl",
    },
    "agy": {
        "default_name": "agy",
        "description": "Google Antigravity CLI executor receiver",
        "token_prefix": "A2A_AGY_TOKEN_",
        "deploy_module": "agy_deploy",
        "transcript_filename": "a2a-agy-transcript.jsonl",
    },
}


def supports_managed_mode(mode: str | None) -> bool:
    """True when ``mode`` is a Hermes-managed receiver mode we understand."""
    return bool(mode in SUPPORTED_MANAGED_MODES)


def managed_peer_default_name(mode: str) -> str:
    """Return the canonical fleet peer name for ``mode``."""
    return _MODE_SPECS[mode]["default_name"]


def managed_peer_description(mode: str, repo_path: str) -> str:
    """Return the default human-readable description for ``mode`` + repo."""
    label = _MODE_SPECS[mode]["description"]
    return f"{label} (repo: {repo_path})"


def transcript_filename_for(mode: str) -> str:
    """Return the transcript JSONL filename written by the receiver for ``mode``.

    Falls back to the claude_code filename for unknown/legacy modes so callers
    that hold an old peer entry without a recognised mode degrade gracefully.
    """
    spec = _MODE_SPECS.get(mode)
    if spec is None:
        return _MODE_SPECS["claude_code"]["transcript_filename"]
    return spec["transcript_filename"]


def canonicalize_managed_repo_path(repo_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve ``repo_path`` to the real on-disk directory used for managed peers."""
    if isinstance(repo_path, dict):
        repo_path = repo_path.get("repo_path") or repo_path.get("path") or ""
    if not repo_path or not str(repo_path).strip():
        return None, "repo_path is empty"
    raw = str(repo_path).strip()
    expanded = os.path.expanduser(raw)
    real = os.path.realpath(expanded)
    if not os.path.exists(real):
        return None, f"repo_path does not exist: {raw}"
    if not os.path.isdir(real):
        return None, f"repo_path is not a directory: {raw}"
    return Path(real), None


def stable_token_env_name(mode: str, repo_path: str | Path) -> str:
    """Return the stable inbound-token env var name for a managed peer mode."""
    if not supports_managed_mode(mode):
        raise ValueError(f"unsupported managed peer mode: {mode!r}")
    repo, _ = canonicalize_managed_repo_path(str(repo_path))
    repo_for_hash = repo or Path(str(repo_path))
    resolver = _deploy_module_stable_token_resolver(mode)
    if resolver is not None:
        return resolver(repo_for_hash)
    return _fallback_stable_token_env_name(mode, repo_for_hash)


def is_supported_managed_peer(entry: Dict[str, Any] | None) -> bool:
    """True when a peer entry is a managed Claude Code or OpenCode receiver."""
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("managed") is True and supports_managed_mode(entry.get("mode")) and entry.get("repo_path"))


def iter_supported_managed_peers(
    agents: Dict[str, Dict[str, Any]] | Iterable[Tuple[str, Dict[str, Any]]],
) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield only the managed peers whose modes are owned by Hermes deployers."""
    items = agents.items() if isinstance(agents, dict) else agents
    for name, entry in items:
        if is_supported_managed_peer(entry):
            yield name, entry


def _deploy_module_stable_token_resolver(mode: str):
    module_name = _MODE_SPECS[mode]["deploy_module"]
    try:
        module = importlib.import_module(f".{module_name}", __package__)
    except Exception:
        return None
    resolver = getattr(module, "stable_token_env_name", None)
    return resolver if callable(resolver) else None


def _fallback_stable_token_env_name(mode: str, repo: Path) -> str:
    canonical = str(repo)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", repo.name).strip("_").upper() or "REPO"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12].upper()
    return f"{_MODE_SPECS[mode]['token_prefix']}{slug}_{digest}"
