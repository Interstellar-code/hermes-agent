"""hermes-switch-ui — Hermes plugin for SwitchUI browser frontend awareness.

Provides:
  - pre_llm_call hook: injects a one-paragraph nudge on first LLM call per session
  - switchui_info tool: returns capability doc + optional live manifest merge
  - switchui_status tool: returns connection info + live status (best-effort)
  - switchui skill: resolvable as 'hermes-switch-ui:switchui'

Phase 3 adds _state.py (sync API + persistence); _knowledge.py already imports
it defensively so this file works without it.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict

# ---------------------------------------------------------------------------
# sys.path injection — ensures absolute imports within this plugin resolve
# (web_server loads plugin_api.py with spec_from_file_location; __init__.py
# is loaded by the plugin loader which sets up sys.path, but be explicit here
# so the plugin works in any load context).
# ---------------------------------------------------------------------------
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import _knowledge  # noqa: E402 — must come after sys.path injection

log = logging.getLogger(__name__)

_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Per-turn nudge (injected once per session on first LLM call)
# ---------------------------------------------------------------------------

_NUDGE = (
    "SwitchUI is the primary browser frontend for this Hermes agent "
    "(React 19 + TanStack Start/Router, Hono BFF on port 3002). "
    "It connects to the Hermes API gateway on port 8642 (HERMES_API_URL) "
    "and the dashboard on port 9119. "
    "Features: chat, dashboard, files, terminal, memory, Matrix3D. "
    "Repo: https://github.com/Interstellar-code/hermes-switchui. "
    "Use the switchui_info tool for full capability details, "
    "or switchui_status for live connection/running state."
)

# Track sessions that have already received the nudge (in-process only)
_nudged_sessions: set = set()

# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------

def _pre_llm_call(session_id: str = "", **kwargs) -> Dict[str, Any] | None:
    """Inject SwitchUI context nudge once per session."""
    if session_id in _nudged_sessions:
        return None
    _nudged_sessions.add(session_id)
    return {"context": _NUDGE}

# ---------------------------------------------------------------------------
# Tool handlers — receive whole args dict as first positional argument
# ---------------------------------------------------------------------------

def _tool_switchui_info(args: dict, **kwargs) -> dict:
    """Return capability doc + optional live manifest merge."""
    refresh = bool(args.get("refresh", False))
    return _knowledge.get_info(refresh=refresh)


def _tool_switchui_status(args: dict, **kwargs) -> dict:
    """Return connection info + live status (best-effort)."""
    return _knowledge.connection_info()

# ---------------------------------------------------------------------------
# register(ctx) — called by Hermes plugin loader at startup
# ---------------------------------------------------------------------------

def register(ctx) -> None:  # noqa: ANN001
    """Register hook, tools, and skill with the Hermes plugin context."""

    # 1. pre_llm_call hook — injects nudge once per session
    ctx.register_hook("pre_llm_call", _pre_llm_call)

    # 2. switchui_info tool
    ctx.register_tool(
        name="switchui_info",
        toolset="hermes-switch-ui",
        schema={
            "type": "object",
            "properties": {
                "refresh": {
                    "type": "boolean",
                    "description": (
                        "If true, attempt a best-effort refresh of capability info "
                        "from SWITCHUI_DOCS_URL (swallows all network errors)."
                    ),
                },
            },
            "additionalProperties": False,
        },
        handler=_tool_switchui_info,
        description="Return SwitchUI capability information (repo, ports, env vars, features).",
        emoji="🖥️",
    )

    # 3. switchui_status tool
    ctx.register_tool(
        name="switchui_status",
        toolset="hermes-switch-ui",
        schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_tool_switchui_status,
        description="Return SwitchUI connection info and live status (ports, active profile, enabled plugins).",
        emoji="📡",
    )

    # 4. switchui skill — guarded: register_skill arrived in a later PluginContext version
    if hasattr(ctx, "register_skill"):
        try:
            ctx.register_skill(
                name="switchui",
                path=_PLUGIN_DIR / "skills" / "switchui" / "SKILL.md",
                description="Guidance on starting, configuring, and troubleshooting the SwitchUI frontend.",
            )
            log.debug("hermes-switch-ui: registered skill 'switchui'")
        except Exception:  # noqa: BLE001 — additive, never break register()
            log.debug("hermes-switch-ui: register_skill failed", exc_info=True)

    log.info(
        "hermes-switch-ui %s: registered pre_llm_call hook + switchui_info/switchui_status tools",
        _VERSION,
    )
