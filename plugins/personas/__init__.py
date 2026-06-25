"""personas — canonical persona store + runtime tools + persona_ref binding.

Hermes owns personas as a first-class runtime capability (a specialized lens
overlaid on an agent's stable identity). This plugin is the single source of
truth for the persona library; SwitchUI becomes a thin client over the REST API.

Provides:
  - persona_list  tool: persona metadata (id/name/category/...), optional category filter
  - persona_get   tool: full persona incl. system_prompt
  - persona_apply tool: composed overlay text + metadata, formatted for a target
  - pre_llm_call hook: resolves the active profile's `agent.persona_ref` into a
    TRUSTED system overlay ({"context", "target": "developer"}). Dormant until a
    profile carries persona_ref — returns None on the common path (cache-warm).

The REST API (dashboard/plugin_api.py) auto-mounts at /api/plugins/personas/.

#140-safe injection contract (agent/conversation_loop.py:722-739):
  target in ("system","developer")  -> appended to effective_system (trusted)
  target absent / "user_message"    -> injected into the USER message (UNTRUSTED)
Persona text is identity-shaping; it MUST use target="developer". Never return a
bare string or user_message target for persona content (that was the #140 vector).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# sys.path injection — make absolute imports within this plugin resolve in any
# load context (loader sets this up, but plugin_api loads flat — be explicit).
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import _library  # noqa: E402 — must follow sys.path injection

log = logging.getLogger(__name__)

_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Overlay composition (shared by persona_apply tool + pre_llm_call hook)
# ---------------------------------------------------------------------------

def _compose_overlay(persona: Dict[str, Any]) -> str:
    """Render a persona into the overlay text appended on top of identity."""
    name = persona.get("name", persona.get("id", "persona"))
    body = persona.get("system_prompt", "").strip()
    return f"## Active persona lens: {name}\n\n{body}"


def _read_persona_ref() -> Optional[str]:
    """Read the active profile's agent.persona_ref from config (best-effort).

    Returns None when unset or config is unavailable — the common path that
    keeps the cached system prefix byte-stable.
    """
    try:
        from hermes_cli.config import cfg_get, load_config  # type: ignore[import]
    except Exception:
        return None
    try:
        ref = cfg_get(load_config(), "agent", "persona_ref", default="")
    except Exception:
        return None
    ref = str(ref or "").strip()
    return ref or None


# ---------------------------------------------------------------------------
# pre_llm_call hook — promoted-profile persona_ref binding (dormant until set)
# ---------------------------------------------------------------------------

def _pre_llm_call(session_id: str = "", **kwargs) -> Optional[Dict[str, Any]]:
    """Resolve agent.persona_ref into a TRUSTED system overlay.

    Fail-open: any problem returns None so the LLM call proceeds unchanged.
    """
    try:
        ref = _read_persona_ref()
        if not ref:
            return None  # common path — no persona bound, cache stays warm
        persona = _library.get_persona(ref)
        if persona is None:
            log.warning("[personas] persona_ref '%s' not found in library", ref)
            return None
        return {"context": _compose_overlay(persona), "target": "developer"}
    except Exception:  # noqa: BLE001 — never break the LLM call
        log.debug("[personas] pre_llm_call failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Tool handlers — receive the whole args dict as the first positional argument
# ---------------------------------------------------------------------------

def _tool_persona_list(args: dict, **kwargs) -> dict:
    """List persona metadata (no full prompt). Optional category filter."""
    category = args.get("category")
    category = str(category).strip() if isinstance(category, str) and category.strip() else None
    personas = _library.list_personas(category=category)
    return {"personas": personas, "count": len(personas)}


def _tool_persona_get(args: dict, **kwargs) -> dict:
    """Return the full persona (incl. system_prompt) by id."""
    persona_id = str(args.get("persona_id", "")).strip()
    if not persona_id:
        return {"error": "persona_id is required"}
    persona = _library.get_persona(persona_id)
    if persona is None:
        return {"error": f"persona '{persona_id}' not found"}
    return {"persona": persona}


def _tool_persona_apply(args: dict, **kwargs) -> dict:
    """Return the composed overlay text + metadata for the caller to inject.

    target="delegate" (default) -> overlay formatted for a delegate_task goal/
    context block (the ephemeral T3 path). This tool does NOT mutate any
    config.yaml; promotion writes are handled by the wizard/promotion path.
    """
    persona_id = str(args.get("persona_id", "")).strip()
    target = str(args.get("target", "delegate")).strip() or "delegate"
    if not persona_id:
        return {"error": "persona_id is required"}
    persona = _library.get_persona(persona_id)
    if persona is None:
        return {"error": f"persona '{persona_id}' not found"}
    return {
        "persona_id": persona_id,
        "name": persona["name"],
        "target": target,
        "overlay": _compose_overlay(persona),
        "default_model": persona.get("default_model"),
        "suggested_mcps": persona.get("suggested_mcps", []),
        "suggested_toolsets": persona.get("suggested_toolsets", []),
    }


# ---------------------------------------------------------------------------
# register(ctx) — called by the Hermes plugin loader at startup
# ---------------------------------------------------------------------------

def register(ctx) -> None:  # noqa: ANN001
    """Register the persona_ref hook + 3 runtime tools (+ optional skill)."""

    ctx.register_hook("pre_llm_call", _pre_llm_call)

    ctx.register_tool(
        name="persona_list",
        toolset="personas",
        schema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Optional category filter (e.g. engineering, design, leadership).",
                },
            },
            "additionalProperties": False,
        },
        handler=_tool_persona_list,
        description="List available personas (metadata: id, name, category, tags, suggested model/mcps/toolsets).",
        emoji="🎭",
    )

    ctx.register_tool(
        name="persona_get",
        toolset="personas",
        schema={
            "type": "object",
            "properties": {
                "persona_id": {"type": "string", "description": "Persona id, e.g. engineering-security-engineer."},
            },
            "required": ["persona_id"],
            "additionalProperties": False,
        },
        handler=_tool_persona_get,
        description="Get a persona's full definition including its system_prompt overlay text.",
        emoji="🎭",
    )

    ctx.register_tool(
        name="persona_apply",
        toolset="personas",
        schema={
            "type": "object",
            "properties": {
                "persona_id": {"type": "string", "description": "Persona id to apply."},
                "target": {
                    "type": "string",
                    "enum": ["delegate"],
                    "description": "Injection target. 'delegate' formats the overlay for a delegate_task goal/context block.",
                },
            },
            "required": ["persona_id"],
            "additionalProperties": False,
        },
        handler=_tool_persona_apply,
        description="Compose a persona overlay for injection (e.g. into a delegate_task goal). Returns overlay text + metadata; does not mutate config.",
        emoji="🎭",
    )

    # Optional operator skill — guarded (register_skill arrived in a later ctx version).
    if hasattr(ctx, "register_skill"):
        skill_path = _PLUGIN_DIR / "skills" / "personas" / "SKILL.md"
        if skill_path.exists():
            try:
                ctx.register_skill(
                    name="personas",
                    path=skill_path,
                    description="How the persona library, runtime tools, and persona_ref binding work.",
                )
            except Exception:  # noqa: BLE001 — additive, never break register()
                log.debug("[personas] register_skill failed", exc_info=True)

    log.info(
        "personas %s: registered pre_llm_call hook + persona_list/get/apply tools (%d personas)",
        _VERSION, _library.count(),
    )
