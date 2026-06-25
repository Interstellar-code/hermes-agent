"""mcp_lazy — lazy MCP tool schema loading.

Phase 1: stub MCP tool schemas at request time, promote on demand via
the ``load_mcp_tools`` meta-tool. Per-session pool keeps each
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
from . import meta_tool_server

logger = logging.getLogger(__name__)


def _get_discovery_mode() -> str:
    """Read discovery_mode from config, default 'tool' (Q9 rollback discipline)."""
    try:
        from hermes_cli.config import load_config  # noqa: PLC0415
        mode = load_config().get("mcp", {}).get("discovery_mode", "tool") or "tool"
        if mode not in {"tool", "server", "both"}:
            return "tool"
        return str(mode)
    except Exception:
        return "tool"


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

    discovery_mode = _get_discovery_mode()
    registered_tools = ["load_mcp_tools"]

    if discovery_mode in {"server", "both"}:
        ctx.register_tool(
            name="load_mcp_server",
            toolset="mcp_lazy",
            schema=meta_tool_server.SCHEMA,
            handler=meta_tool_server.handler,
            check_fn=meta_tool_server.check,
            is_async=True,
            description=meta_tool_server.SCHEMA.get("description", ""),
            emoji="🗂️",
        )
        registered_tools.append("load_mcp_server")

    ctx.register_hook("transform_tools", hook_impl.transform_tools)
    ctx.register_hook("on_session_reset", hook_impl.on_session_reset)

    # Register pre_tool_call for auto-promote-on-stub-call (CRITICAL #1).
    # Hook is invoked by the dispatcher via get_pre_tool_call_block_message;
    # our handler returns a block directive when a stub call is detected so
    # the model sees a "promoted, retry next turn" message instead of a
    # schema-validation error from the MCP server.
    try:
        ctx.register_hook("pre_tool_call", hook_impl.pre_tool_call)
    except Exception:
        logger.debug("mcp_lazy: pre_tool_call hook registration failed", exc_info=True)

    logger.info(
        "mcp_lazy: discovery_mode=%r, registered: %s",
        discovery_mode,
        registered_tools,
    )


# Register the baseline observer the moment the plugin is imported.
# The Hermes plugin loader imports each enabled plugin's top-level
# package, so this import-time side effect is sufficient for the
# passive Phase 0 logger. The interactive Phase 1 surface (meta-tool
# + hooks) registers via the ctx-driven ``register()`` callback above.
try:
    baseline_patch.install()
except Exception:
    logger.warning("mcp_lazy: failed to install baseline observer", exc_info=True)
