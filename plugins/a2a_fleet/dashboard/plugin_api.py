"""a2a_fleet dashboard API — read-only A2A conversation feed.

Surfaces the back-and-forth between Hermes (orchestrator) and the deployed
Claude Code executor receivers, sourced from each managed peer's per-repo
transcript (``<repo>/.hermes/a2a-transcript.jsonl`` — one JSON line per message,
both directions, including the ``[queued]`` ack and the real reply).

Mounted by web_server._mount_plugin_api_routes() under ``/api/plugins/a2a_fleet``
(bundled plugin → backend auto-imports; project plugins do not). Behind the
dashboard session auth like every other ``/api/plugins/*`` route — read-only GETs.

Endpoints (front-end polls these; a 2s interval is plenty for a live tab):
  GET /api/plugins/a2a_fleet/conversations
      -> {"conversations": [{contextId, peer, repo_path, message_count,
                             last_ts, last_dir, last_text}], "generated_ts": ...}
  GET /api/plugins/a2a_fleet/conversations/{context_id}
      -> {"contextId", "peer", "repo_path",
          "messages": [{ts, dir, from, to, text}]}
  GET /api/plugins/a2a_fleet/peers
      -> {"peers": [{name, repo_path, transcript_exists, message_count}]}
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query

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
TRANSCRIPT_RELPATH = (".hermes", "a2a-transcript.jsonl")

router = APIRouter()


def _managed_repos() -> List[Tuple[str, str]]:
    """Return [(peer_name, repo_path)] for managed claude_code peers in fleet.yaml.

    Never raises: a missing/invalid fleet.yaml yields an empty list (the tab just
    shows no conversations rather than 500-ing).
    """
    try:
        from a2a_fleet import fleet_config  # noqa: PLC0415
        cfg = fleet_config.load_fleet()
    except Exception as exc:  # noqa: BLE001 — read-only surface; degrade to empty.
        log.debug("a2a_fleet conversations: load_fleet failed: %s", exc)
        return []

    out: List[Tuple[str, str]] = []
    seen: set = set()
    for name, entry in (cfg.get("agents") or {}).items():
        if not isinstance(entry, dict):
            continue
        repo = entry.get("repo_path")
        if entry.get("managed") and entry.get("mode") == "claude_code" and repo:
            key = str(repo)
            if key not in seen:
                seen.add(key)
                out.append((str(name), key))
    return out


def _transcript_path(repo_path: str) -> Path:
    return Path(repo_path).joinpath(*TRANSCRIPT_RELPATH)


def _read_transcript(repo_path: str) -> List[Dict[str, Any]]:
    """Parse a receiver transcript JSONL into a list of message dicts (best-effort)."""
    path = _transcript_path(repo_path)
    msgs: List[Dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8")
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


def _collect() -> Dict[str, Dict[str, Any]]:
    """Build {contextId: {peer, repo_path, messages: [...]}} across all managed peers."""
    convos: Dict[str, Dict[str, Any]] = {}
    for peer_name, repo in _managed_repos():
        for msg in _read_transcript(repo):
            cid = msg.get("contextId") or "(no-context)"
            bucket = convos.get(cid)
            if bucket is None:
                bucket = {"peer": peer_name, "repo_path": repo, "messages": []}
                convos[cid] = bucket
            if len(bucket["messages"]) < _MAX_MESSAGES_PER_CONTEXT:
                # Drop the now-redundant contextId from each message for payload size.
                bucket["messages"].append({k: msg[k] for k in ("ts", "dir", "from", "to", "text")})
    return convos


@router.get("/conversations")
async def list_conversations() -> Dict[str, Any]:
    """Summary of every A2A conversation, newest activity first."""
    convos = _collect()
    out: List[Dict[str, Any]] = []
    for cid, bucket in convos.items():
        msgs = bucket["messages"]
        last = msgs[-1] if msgs else {}
        text = last.get("text") or ""
        out.append({
            "contextId": cid,
            "peer": bucket["peer"],
            "repo_path": bucket["repo_path"],
            "message_count": len(msgs),
            "last_ts": last.get("ts"),
            "last_dir": last.get("dir"),
            "last_text": text[:240],
        })
    # Sort by last_ts desc; None timestamps sink to the bottom.
    out.sort(key=lambda c: (c["last_ts"] or ""), reverse=True)
    return {"conversations": out, "count": len(out)}


@router.get("/conversations/{context_id:path}")
async def get_conversation(context_id: str) -> Dict[str, Any]:
    """Full ordered message list for one conversation (by contextId)."""
    bucket = _collect().get(context_id)
    if bucket is None:
        raise HTTPException(status_code=404, detail=f"no A2A conversation for contextId {context_id!r}")
    return {
        "contextId": context_id,
        "peer": bucket["peer"],
        "repo_path": bucket["repo_path"],
        "messages": bucket["messages"],
    }


@router.get("/peers")
async def list_peers() -> Dict[str, Any]:
    """Managed Claude Code receivers + whether each has a readable transcript."""
    peers: List[Dict[str, Any]] = []
    for name, repo in _managed_repos():
        msgs = _read_transcript(repo)
        peers.append({
            "name": name,
            "repo_path": repo,
            "transcript_exists": _transcript_path(repo).is_file(),
            "message_count": len(msgs),
        })
    return {"peers": peers, "count": len(peers)}
