"""mcp_lazy — lazy MCP tool schema loading.

Phase 0 (this commit): baseline cache hit-rate logger only. No tool
mutation, no schema stubs — purely passive instrumentation that feeds
the Phase 1 design decision.

Activation: standalone plugin, opt-in via ``hermes plugins enable
mcp_lazy``. Disable Phase 0 logging via ``HERMES_MCP_LAZY_BASELINE=0``
in ``~/.hermes/.env`` without disabling the whole plugin.

See ``.omc/plans/mcp-lazy-loading-v4.md`` for the full multi-phase
plan and the issue at Interstellar-code/hermes-agent#5 for context.
"""
from __future__ import annotations

import logging

from . import baseline_patch

logger = logging.getLogger(__name__)

# Register the baseline observer the moment the plugin is imported.
# The Hermes plugin loader imports each enabled plugin's top-level
# package, so this import-time side effect is sufficient.
try:
    baseline_patch.install()
except Exception:
    logger.warning("mcp_lazy: failed to install baseline observer", exc_info=True)
