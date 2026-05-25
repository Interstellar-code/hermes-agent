"""mcp_lazy — lazy MCP tool schema loading.

Phase 1: stub MCP tool schemas at request time, promote on demand via
the ``mcp_load_tools`` meta-tool. Per-session pool keeps each
conversation's promoted set isolated.

Phase 0 baseline logger remains active as passive telemetry (no
behaviour change).

Activation: standalone plugin, opt-in via ``hermes plugins enable
mcp_lazy``. Behaviour gated by ``mcp.lazy_loading: true`` in
``config.yaml``; per-server override via ``mcp_servers.<name>.lazy``.

See ``.omc/plans/mcp-lazy-loading-v4.md`` for the full plan and
Interstellar-code/hermes-agent#5 for context.
"""
from __future__ import annotations

import logging

from . import baseline_patch
from . import hook_impl
from . import meta_tool

logger = logging.getLogger(__name__)


def register(ctx) -> None:  # noqa: ANN001
    """Register the meta-tool + lifecycle hooks with the plugin context."""
    ctx.register_tool(
        name="load_mcp_tools",
        toolset="mcp_lazy",
        schema=meta_tool.SCHEMA,
        handler=meta_tool.handler,
        check_fn=meta_tool.check,
        is_async=True,
        description=meta_tool.SCHEMA.get("description", ""),
        emoji="📦",
    )
    ctx.register_hook("transform_tools", hook_impl.transform_tools)
    ctx.register_hook("on_session_reset", hook_impl.on_session_reset)
    logger.info("mcp_lazy: registered meta-tool + transform_tools/on_session_reset hooks")


# Register the baseline observer the moment the plugin is imported.
# The Hermes plugin loader imports each enabled plugin's top-level
# package, so this import-time side effect is sufficient for the
# passive Phase 0 logger. The interactive Phase 1 surface (meta-tool
# + hooks) registers via the ctx-driven ``register()`` callback above.
try:
    baseline_patch.install()
except Exception:
    logger.warning("mcp_lazy: failed to install baseline observer", exc_info=True)
