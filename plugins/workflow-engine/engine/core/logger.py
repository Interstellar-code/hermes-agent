"""
Per-run workflow logger.

Ports logger.ts: structured logging with run/node context.
In Python we use stdlib logging; the caller can attach handlers.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional


def get_workflow_logger(name: str = "workflow") -> logging.Logger:
    """Return a logger for the workflow engine."""
    return logging.getLogger(name)


def log_node_start(
    log_dir: Optional[str],
    run_id: str,
    node_id: str,
    command: str,
) -> None:
    """Append a node-start entry to the run log file."""
    _append_log(log_dir, run_id, f"[START] node={node_id} command={command!r}")


def log_node_complete(
    log_dir: Optional[str],
    run_id: str,
    node_id: str,
    command: str,
    *,
    duration_ms: int = 0,
    tokens: Optional[int] = None,
) -> None:
    """Append a node-complete entry."""
    extra = f" tokens={tokens}" if tokens is not None else ""
    _append_log(log_dir, run_id, f"[DONE ] node={node_id} duration={duration_ms}ms{extra}")


def log_node_error(
    log_dir: Optional[str],
    run_id: str,
    node_id: str,
    error: str,
) -> None:
    """Append a node-error entry."""
    _append_log(log_dir, run_id, f"[ERROR] node={node_id} error={error!r}")


def log_node_skip(
    log_dir: Optional[str],
    run_id: str,
    node_id: str,
    reason: str,
) -> None:
    """Append a node-skip entry."""
    _append_log(log_dir, run_id, f"[SKIP ] node={node_id} reason={reason}")


def _append_log(log_dir: Optional[str], run_id: str, line: str) -> None:
    """Write a timestamped line to the run log file (best-effort)."""
    if not log_dir:
        return
    try:
        path = Path(log_dir) / f"{run_id}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts} {line}\n")
    except OSError:
        pass
