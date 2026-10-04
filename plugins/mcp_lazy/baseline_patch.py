"""Phase 0 baseline logger for the lazy-MCP project.

Registers a callback with the canonical usage pipeline in
``agent/usage_pricing.py``. Every Anthropic / Codex / Chat-Completions
response that flows through ``normalize_usage()`` is appended to a JSONL
log at ``<HERMES_HOME>/mcp-lazy/cache-baseline.jsonl`` (rotated at 5 MB).

The log feeds the Phase 0 decision (immediate vs deferred promotion)
in ``.omc/plans/mcp-lazy-loading-v4.md`` — we need real cache hit-rate
data before deciding whether mid-session tool-list mutation is safe.

Plugin → core dependency direction: this module imports
``register_usage_observer`` from core. Core has no knowledge of this
file; the observer slot was added precisely so plugins like this one
can attach without core ever naming them.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.usage_pricing import CanonicalUsage

logger = logging.getLogger(__name__)

def _log_file() -> Path:
    from hermes_constants import get_hermes_home  # noqa: PLC0415
    return get_hermes_home() / "mcp-lazy" / "cache-baseline.jsonl"


# ponytail: one-shot truncate-to-.1 rotation; upgrade if analysis needs full history.
_MAX_BYTES = 5 * 1024 * 1024

# Toggle via env so we can disable in CI / tests without touching code.
# Default ON — Phase 0's whole point is "always be logging until we
# have the data to decide Phase 1's promotion strategy".
_ENABLED = os.environ.get("HERMES_MCP_LAZY_BASELINE", "1").strip().lower() not in {
    "0", "false", "no", "off",
}


def _baseline_log(usage: "CanonicalUsage") -> None:
    """Append one JSONL row per canonicalised usage record.

    Payload shape is intentionally minimal — we want raw counters now,
    derived hit-rate later via ``scripts/cache_report.py``. Writing the
    rate at log time would freeze the denominator definition before
    we've validated it.
    """
    if not _ENABLED:
        return
    try:
        log_file = _log_file()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            if log_file.stat().st_size > _MAX_BYTES:
                log_file.replace(log_file.with_suffix(".jsonl.1"))
        except FileNotFoundError:
            pass
        row = {
            "ts": time.time(),
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read": usage.cache_read_tokens,
            "cache_creation": usage.cache_write_tokens,
        }
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        # Logger may itself be broken; never let baseline breakage
        # affect the caller. Use stderr-fallback debug only.
        logger.debug("baseline log write failed", exc_info=True)


def install() -> None:
    """Register the observer with the canonical usage pipeline.

    Called once from the package ``__init__`` at plugin import time.
    Idempotent: re-registration would double-log, so callers should
    only invoke once per process lifetime.
    """
    from agent.usage_pricing import register_usage_observer  # noqa: PLC0415
    register_usage_observer(_baseline_log)
    logger.debug("mcp_lazy baseline observer registered")
