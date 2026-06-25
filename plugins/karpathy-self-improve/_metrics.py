"""
_metrics.py — Per-profile metrics collection for karpathy-self-improve.

P0: derives metrics from agent.log ONLY (errors.log is a strict subset of
agent.log — both files receive the same WARNING+ records via the shared
RotatingFileHandler chain in hermes_logging.py — so counting both would
double-count errors/warnings).

Metrics are collected over a window anchored by byte offsets so that
successive collections can attribute delta counts to a specific window.

Open Question P0: log lines are not profile-tagged yet. All metrics are
written under profile="(unknown)" until the agent runtime begins tagging
log lines with a profile identifier.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Canonical session-start signature anchored on the real log tag emitted by
# hermes_logging.py:set_session_context() / run_agent.py:on_session_start.
# The format is: "... [<session_id>] ..." injected via the session record
# factory; we match the on_session_start call that always accompanies it.
_SESSION_START_RE = re.compile(
    r"on_session_start",
    re.IGNORECASE,
)
_ERROR_RE = re.compile(r"\bERROR\b")
_WARN_RE = re.compile(r"\bWARNING\b|\bWARN\b")

# Regex patterns for token / cost lines if ever present in logs.
# Currently not emitted; kept as stubs for future enrichment.
_TOKEN_RE = re.compile(r"tokens[=:\s]+(\d+)", re.IGNORECASE)
_COST_RE = re.compile(r"cost[=:\s]+\$?([\d.]+)", re.IGNORECASE)
_RETRY_RE = re.compile(r"\bretry\b|\bretrying\b", re.IGNORECASE)


def _read_log(path: Path, from_offset: int = 0) -> tuple[List[str], int]:
    """Read a log file from *from_offset* bytes, returning (lines, end_offset).

    Returns ([], 0) if the file is absent or unreadable.
    The end_offset is the byte position after the last byte read — pass it
    back as from_offset on the next call to get only new lines.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(from_offset)
            data = fh.read()
            end_offset = from_offset + len(data)
        return data.decode(encoding="utf-8", errors="replace").splitlines(), end_offset
    except OSError:
        return [], from_offset


def _parse_log_metrics(
    log_dir: Path,
    from_offset: int = 0,
) -> Dict[str, int | float]:
    """Parse agent.log ONLY (errors.log is a subset) and return raw counts.

    Returns a dict with keys:
      sessions_count, error_count, warn_count, tokens, cost, retries,
      to_offset  (byte position after last read — for window tracking)
    """
    agent_path = log_dir / "agent.log"
    lines, to_offset = _read_log(agent_path, from_offset=from_offset)

    sessions_count = sum(1 for ln in lines if _SESSION_START_RE.search(ln))
    error_count = sum(1 for ln in lines if _ERROR_RE.search(ln))
    warn_count = sum(1 for ln in lines if _WARN_RE.search(ln))
    retries = sum(1 for ln in lines if _RETRY_RE.search(ln))

    tokens = 0
    cost = 0.0
    for ln in lines:
        m = _TOKEN_RE.search(ln)
        if m:
            tokens += int(m.group(1))
        m2 = _COST_RE.search(ln)
        if m2:
            cost += float(m2.group(1))

    return {
        "sessions_count": sessions_count,
        "error_count": error_count,
        "warn_count": warn_count,
        "tokens": tokens,
        "cost": cost,
        "retries": retries,
        "to_offset": to_offset,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_profile_metrics(
    log_dir: Optional[Path] = None,
    from_offset: int = 0,
) -> List[Dict]:
    """
    Collect metrics from agent.log and write one metrics_snapshots row.

    Returns the list of inserted snapshot dicts (one per profile — P0 always
    returns a single entry with profile="(unknown)").

    The *log_dir* parameter allows tests to inject a custom log directory;
    defaults to get_hermes_home() / "logs".

    *from_offset* is the byte offset to start reading agent.log from.  Pass
    the ``to_offset`` from the previous snapshot to get only the delta window.
    The returned dict includes ``from_offset`` and ``to_offset`` fields so
    callers can persist and re-pass them.

    NOTE(#133-Q1): Log lines are not profile-tagged in P0. All metrics are
    recorded under profile="(unknown)". Set needs_profile_tagging=True in
    the returned dict as a signal to callers. This function does NOT block on
    unresolved profile tagging — it returns usable data immediately.
    """
    # Import here to avoid circular import at module load time.
    from _db import get_db  # absolute import; sys.path set by plugin loader

    if log_dir is None:
        log_dir = get_hermes_home() / "logs"

    window_started_at = datetime.now(timezone.utc).isoformat()
    counts = _parse_log_metrics(log_dir, from_offset=from_offset)
    to_offset = counts.pop("to_offset")
    window_ended_at = datetime.now(timezone.utc).isoformat()

    profile = "(unknown)"
    db = get_db()
    row_id = db.insert_metrics_snapshot(
        profile=profile,
        captured_at=window_started_at,
        sessions_count=counts["sessions_count"],
        error_count=counts["error_count"],
        warn_count=counts["warn_count"],
        tokens=counts["tokens"],
        cost=counts["cost"],
        retries=counts["retries"],
        window_started_at=window_started_at,
        window_ended_at=window_ended_at,
        from_offset=from_offset,
        to_offset=to_offset,
        payload={"source": "agent.log", "log_dir": str(log_dir)},
    )
    logger.debug(
        "karpathy-self-improve: metrics snapshot id=%s profile=%s offsets=%d..%d",
        row_id,
        profile,
        from_offset,
        to_offset,
    )

    snapshot = {
        "id": row_id,
        "profile": profile,
        "captured_at": window_started_at,
        **counts,
        "from_offset": from_offset,
        "to_offset": to_offset,
        "needs_profile_tagging": True,  # TODO(#133-Q1)
    }
    return [snapshot]
