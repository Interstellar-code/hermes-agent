"""a2a_fleet dashboard API — read-only A2A conversation feed.

Surfaces the back-and-forth between Hermes (orchestrator) and the deployed
managed executor receivers (Claude Code, OpenCode, Codex CLI, agy), sourced
from each managed peer's per-repo transcript (one JSON line per message, both
directions, including the ``[queued]`` ack and the real reply).  Each mode
writes a DISTINCT transcript file; the filename is resolved via
``managed_peers.transcript_filename_for(mode)``.

Mounted by web_server._mount_plugin_api_routes() under ``/api/plugins/a2a_fleet``
(bundled plugin → backend auto-imports; project plugins do not). Behind the
dashboard session auth like every other ``/api/plugins/*`` route — read-only GETs.

Endpoints (front-end polls these; a 2s interval is plenty for a live tab):
  GET /api/plugins/a2a_fleet/conversations
      -> {"conversations": [{contextId, peer, repo_path, mode, message_count,
                             last_ts, last_dir, last_text}], "count": ...}
  GET /api/plugins/a2a_fleet/conversations/{context_id}
      -> {"contextId", "peer", "repo_path", "mode",
          "messages": [{ts, dir, from, to, text}]}
  GET /api/plugins/a2a_fleet/peers
      -> {"peers": [{name, repo_path, mode, transcript_exists, message_count}]}
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException

log = logging.getLogger(__name__)

# web_server loads this file via spec_from_file_location as a FLAT module (no
# parent package), so ``from ..fleet_config`` would fail. Put the plugins/ root
# on sys.path and import a2a_fleet as a real PACKAGE so fleet_config's own
# relative imports (``from .cc_deploy import ...``) resolve.
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_PLUGINS_ROOT = _Path(__file__).resolve().parent.parent.parent  # plugins/
if str(_PLUGINS_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_PLUGINS_ROOT))
del _sys, _Path

# Cap a single transcript read so a runaway log can't balloon a response.
_MAX_MESSAGES_PER_CONTEXT = 2000
# Above this size, read only the TAIL (bounds memory/latency before the per-context
# cap kicks in — the cap alone fires after the whole file is already in memory).
_MAX_TRANSCRIPT_BYTES = 5_000_000  # ~5 MB
# Legacy constant kept for back-compat; new code uses transcript_filename_for(mode).
TRANSCRIPT_RELPATH = (".hermes", "a2a-transcript.jsonl")

router = APIRouter()


def _fleet_yaml_candidates() -> List[Path]:
    """Every fleet.yaml the dashboard should consider, across ALL profiles.

    The dashboard is a global control plane — it typically runs under the default
    Hermes home (``~/.hermes``) while the managed receivers live in a specific
    profile (e.g. ``~/.hermes/profiles/hermes-switch/fleet.yaml``). Binding this
    endpoint to the dashboard's own profile would make it perpetually empty. So we
    scan the home's own ``fleet.yaml`` AND every ``profiles/*/fleet.yaml`` beneath
    it — surfacing A2A conversations regardless of which profile owns the receiver.
    """
    try:
        from hermes_constants import get_hermes_home  # noqa: PLC0415
        home = get_hermes_home()
    except Exception:  # noqa: BLE001
        return []
    candidates: List[Path] = [home / "fleet.yaml"]
    profiles_dir = home / "profiles"
    try:
        if profiles_dir.is_dir():
            for child in sorted(profiles_dir.iterdir()):
                if child.is_dir():
                    candidates.append(child / "fleet.yaml")
    except OSError:
        pass
    return [c for c in candidates if c.is_file()]


def _managed_repos() -> List[Tuple[str, str, str]]:
    """Return ``[(peer_name, repo_path, mode)]`` for ALL managed peers across all
    profiles' fleet.yaml — deduped by ``(repo_path, mode)``.

    Covers every mode in ``managed_peers.SUPPORTED_MANAGED_MODES`` (claude_code,
    opencode, codex, agy) so the dashboard surfaces the full fleet, not just
    Claude Code receivers. Dedup MUST be keyed by ``(repo_path, mode)`` and not
    by ``repo_path`` alone: multiple modes legitimately coexist in one repo
    (e.g. a cc + codex peer for the same repo), and a repo-only key silently
    drops every non-first mode (issue #95).

    Profile-agnostic (see :func:`_fleet_yaml_candidates`) so a global dashboard
    surfaces every profile's receivers. Parses raw YAML leniently — no token_env /
    schema validation (that is ``load_fleet``'s job for the live server, and a
    validation error must not blank the read-only feed). Never raises: any
    unreadable/invalid file is skipped and yields no peers.
    """
    import yaml  # noqa: PLC0415
    from a2a_fleet.managed_peers import supports_managed_mode  # noqa: PLC0415

    out: List[Tuple[str, str, str]] = []
    seen: set = set()
    for path in _fleet_yaml_candidates():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            agents = ((raw.get("fleet") or {}).get("agents")) or {}
        except Exception as exc:  # noqa: BLE001 — read-only surface; skip bad files.
            log.debug("a2a_fleet conversations: skipping %s: %s", path, exc)
            continue
        if not isinstance(agents, dict):
            continue
        for name, entry in agents.items():
            if not isinstance(entry, dict):
                continue
            repo = entry.get("repo_path")
            mode = entry.get("mode") or ""
            if entry.get("managed") is True and supports_managed_mode(mode) and repo:
                repo_str = str(repo)
                key = (repo_str, mode)  # (repo, mode) — NOT repo alone (issue #95)
                if key not in seen:
                    seen.add(key)
                    out.append((str(name), repo_str, mode))
    return out


def _transcript_path(repo_path: str, mode: str = "claude_code") -> Path:
    """Return the transcript path for ``repo_path`` + ``mode``.

    Uses the per-mode filename from ``managed_peers.transcript_filename_for``
    so each executor type's transcript is read correctly.  Falls back to the
    claude_code filename for unknown/legacy modes (graceful back-compat).
    """
    from a2a_fleet.managed_peers import transcript_filename_for  # noqa: PLC0415
    filename = transcript_filename_for(mode)
    return Path(repo_path) / ".hermes" / filename


def _read_transcript(repo_path: str, mode: str = "claude_code") -> List[Dict[str, Any]]:
    """Parse a receiver transcript JSONL into a list of message dicts (best-effort)."""
    path = _transcript_path(repo_path, mode)
    msgs: List[Dict[str, Any]] = []
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            if size > _MAX_TRANSCRIPT_BYTES:
                # Seek to the tail and drop the first (likely partial) line so a
                # huge transcript can't balloon memory before the per-context cap.
                fh.seek(size - _MAX_TRANSCRIPT_BYTES)
                fh.readline()
            raw = fh.read()
    except OSError:
        return msgs
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        msgs.append({
            "ts": rec.get("ts"),
            "dir": rec.get("dir"),
            "from": rec.get("from"),
            "to": rec.get("to"),
            "contextId": rec.get("contextId"),
            "text": rec.get("text"),
        })
    return msgs


def _collect() -> List[Dict[str, Any]]:
    """Build a list of conversation buckets across all managed peers.

    Keyed by ``(repo_path, contextId)`` — NOT contextId alone: two different repos
    can legitimately reuse the same contextId (e.g. ``handshake:<profile>``), and
    merging them would misattribute one repo's transcript text to another. Each
    bucket is therefore scoped to a single repo. Insertion order is preserved.
    """
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str]] = []
    for peer_name, repo, mode in _managed_repos():
        for msg in _read_transcript(repo, mode):
            cid = msg.get("contextId") or "(no-context)"
            key = (repo, cid)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = {
                    "contextId": cid,
                    "peer": peer_name,
                    "repo_path": repo,
                    "mode": mode,
                    "messages": [],
                }
                buckets[key] = bucket
                order.append(key)
            if len(bucket["messages"]) < _MAX_MESSAGES_PER_CONTEXT:
                # Drop the now-redundant contextId from each message for payload size.
                bucket["messages"].append({k: msg[k] for k in ("ts", "dir", "from", "to", "text")})
    return [buckets[k] for k in order]


@router.get("/conversations")
async def list_conversations() -> Dict[str, Any]:
    """Summary of every A2A conversation, newest activity first."""
    out: List[Dict[str, Any]] = []
    for bucket in _collect():
        msgs = bucket["messages"]
        last = msgs[-1] if msgs else {}
        text = last.get("text") or ""
        out.append({
            "contextId": bucket["contextId"],
            "peer": bucket["peer"],
            "repo_path": bucket["repo_path"],
            "mode": bucket["mode"],
            "message_count": len(msgs),
            "last_ts": last.get("ts"),
            "last_dir": last.get("dir"),
            "last_text": text[:240],
        })
    # Sort by last_ts desc; None timestamps sink to the bottom.
    out.sort(key=lambda c: (c["last_ts"] or ""), reverse=True)
    return {"conversations": out, "count": len(out)}


@router.get("/conversations/{context_id:path}")
async def get_conversation(
    context_id: str,
    peer: Optional[str] = None,
    repo_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Full ordered message list for one conversation.

    A contextId can be shared across repos, so the match may be ambiguous. Narrow
    with ``?peer=`` / ``?repo_path=`` when it is; an ambiguous bare lookup returns
    409 with the candidate peers/repos rather than silently merging them.
    """
    matches = [
        b for b in _collect()
        if b["contextId"] == context_id
        and (peer is None or b["peer"] == peer)
        and (repo_path is None or b["repo_path"] == repo_path)
    ]
    if not matches:
        raise HTTPException(status_code=404, detail=f"no A2A conversation for contextId {context_id!r}")
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "error": f"contextId {context_id!r} matches multiple peers; narrow with ?peer= or ?repo_path=",
                "candidates": [{"peer": b["peer"], "repo_path": b["repo_path"]} for b in matches],
            },
        )
    bucket = matches[0]
    return {
        "contextId": bucket["contextId"],
        "peer": bucket["peer"],
        "repo_path": bucket["repo_path"],
        "mode": bucket["mode"],
        "messages": bucket["messages"],
    }


@router.get("/peers")
async def list_peers() -> Dict[str, Any]:
    """All managed receivers (every mode) + whether each has a readable transcript."""
    peers: List[Dict[str, Any]] = []
    for name, repo, mode in _managed_repos():
        msgs = _read_transcript(repo, mode)
        peers.append({
            "name": name,
            "repo_path": repo,
            "mode": mode,
            "transcript_exists": _transcript_path(repo, mode).is_file(),
            "message_count": len(msgs),
        })
    return {"peers": peers, "count": len(peers)}
