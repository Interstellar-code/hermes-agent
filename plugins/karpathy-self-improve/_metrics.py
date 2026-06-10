"""
_metrics.py — Per-profile metrics collection for karpathy-self-improve.

P0: derives metrics from log files under get_hermes_home()/"logs/":
  - agent.log   : INFO lines, session boundaries
  - errors.log  : ERROR / WARNING lines

Open Question #133-Q1: log lines are NOT profile-tagged in P0.
All metrics are written with profile="(unknown)" until the agent runtime
begins tagging log lines with a profile identifier.
# TODO(#133-Q1): replace "(unknown)" profile once log lines carry profile tags.

Structure is intentionally thin so a richer source (gateway sessions store,
token usage DB) can replace the log-parsing path without changing callers.
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
# Helpers
# ---------------------------------------------------------------------------

_SESSION_START_RE = re.compile(
    r"on_session_start|session[_\s]start|run_agent:.*agent_init",
    re.IGNORECASE,
)
_ERROR_RE = re.compile(r"\bERROR\b")
_WARN_RE = re.compile(r"\bWARNING\b|\bWARN\b")

# Regex patterns for token / cost lines if ever present in logs.
# Currently not emitted; kept as stubs for future enrichment.
_TOKEN_RE = re.compile(r"tokens[=:\s]+(\d+)", re.IGNORECASE)
_COST_RE = re.compile(r"cost[=:\s]+\$?([\d.]+)", re.IGNORECASE)
_RETRY_RE = re.compile(r"\bretry\b|\bretrying\b", re.IGNORECASE)


def _read_log(path: Path) -> List[str]:
    """Read a log file, returning lines. Returns [] if file absent or unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _parse_log_metrics(log_dir: Path) -> Dict[str, int | float]:
    """
    Parse agent.log + errors.log and return raw counts.

    Returns a dict with keys:
      sessions_count, error_count, warn_count, tokens, cost, retries
    """
    agent_lines = _read_log(log_dir / "agent.log")
    error_lines = _read_log(log_dir / "errors.log")
    all_lines = agent_lines + error_lines

    sessions_count = sum(1 for ln in agent_lines if _SESSION_START_RE.search(ln))
    error_count = sum(1 for ln in all_lines if _ERROR_RE.search(ln))
    warn_count = sum(1 for ln in all_lines if _WARN_RE.search(ln))
    retries = sum(1 for ln in all_lines if _RETRY_RE.search(ln))

    tokens = 0
    cost = 0.0
    for ln in all_lines:
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
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_profile_metrics(
    log_dir: Optional[Path] = None,
) -> List[Dict]:
    """
    Collect metrics from log files and write one metrics_snapshots row.

    Returns the list of inserted snapshot dicts (one per profile — P0 always
    returns a single entry with profile="(unknown)").

    The *log_dir* parameter allows tests to inject a custom log directory;
    defaults to get_hermes_home() / "logs".

    NOTE(#133-Q1): Log lines are not profile-tagged in P0. All metrics are
    recorded under profile="(unknown)". Set needs_profile_tagging=True in
    the returned dict as a signal to callers. This function does NOT block on
    unresolved profile tagging — it returns usable data immediately.
    """
    # Import here to avoid circular import at module load time.
    from _db import get_db  # absolute import; sys.path set by plugin loader

    if log_dir is None:
        log_dir = get_hermes_home() / "logs"

    captured_at = datetime.now(timezone.utc).isoformat()
    counts = _parse_log_metrics(log_dir)

    profile = "(unknown)"
    db = get_db()
    row_id = db.insert_metrics_snapshot(
        profile=profile,
        captured_at=captured_at,
        sessions_count=counts["sessions_count"],
        error_count=counts["error_count"],
        warn_count=counts["warn_count"],
        tokens=counts["tokens"],
        cost=counts["cost"],
        retries=counts["retries"],
        payload={"source": "log_files", "log_dir": str(log_dir)},
    )
    logger.debug(
        "karpathy-self-improve: metrics snapshot id=%s profile=%s", row_id, profile
    )

    snapshot = {
        "id": row_id,
        "profile": profile,
        "captured_at": captured_at,
        **counts,
        "needs_profile_tagging": True,  # TODO(#133-Q1)
    }
    return [snapshot]
