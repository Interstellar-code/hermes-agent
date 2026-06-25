"""Phase 0 baseline logger for the lazy-MCP project.

Registers a callback with the canonical usage pipeline in
``agent/usage_pricing.py``. Every Anthropic / Codex / Chat-Completions
response that flows through ``normalize_usage()`` is appended to a JSONL
log at ``~/.hermes/mcp-lazy/cache-baseline.jsonl``.

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

_LOG_DIR_DEFAULT = Path.home() / ".hermes" / "mcp-lazy"
_LOG_FILE = _LOG_DIR_DEFAULT / "cache-baseline.jsonl"

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
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": time.time(),
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read": usage.cache_read_tokens,
            "cache_creation": usage.cache_write_tokens,
        }
        with _LOG_FILE.open("a", encoding="utf-8") as f:
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
