"""Hook implementations for the mcp_lazy plugin.

* ``transform_tools`` — rewrites the agent's tool list at request time,
  stubbing MCP tools that haven't been promoted in this session.
* ``on_session_reset`` — drops the per-session pool when the user
  starts a new session (via ``/new`` or its ``/reset`` alias).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set

from .pool import _current_agent_var, evict, get_pool
from .stubs import mix_full_and_stubs, is_stub_schema, _server_in_set
from .server_stubs import is_server_stub_schema

logger = logging.getLogger(__name__)

# NOTE: _prev_mode tracking has been moved into DeferredToolPool._prev_mode so
# that it is cleared automatically when the session pool is evicted.  The
# module-level dict that existed here leaked entries across sessions and was
# never cleaned up on evict().  See Interstellar-code/hermes-agent#29.


def _load_config() -> Dict[str, Any]:
    """Read ``mcp`` config block; tolerate missing config."""
    try:
        from hermes_cli.config import load_config  # noqa: PLC0415
        return load_config().get("mcp", {}) or {}
    except Exception:
        return {}


def _lazy_mode(cfg: Dict[str, Any]) -> str:
    """Normalize ``mcp.lazy_loading`` to 'off' | 'on' | 'auto'.

    Accepts booleans (legacy) and the strings on/off/auto/true/false.
    Unknown truthy values mean 'on' so legacy ``lazy_loading: true``
    configs keep working; note the strings "off"/"false" are truthy
    in Python, hence the explicit check.
    """
    val = cfg.get("lazy_loading")
    if isinstance(val, str):
        v = val.strip().lower()
        if v == "auto":
            return "auto"
        if v in {"off", "false", "no", ""}:
            return "off"
        return "on"
    return "on" if val else "off"


def _schema_tokens(schema: Dict[str, Any]) -> int:
    """Cheap tokenizer-free schema cost estimate (chars/4)."""
    try:
        return len(json.dumps(schema)) // 4
    except Exception:
        return 0


def _eligible_servers() -> Optional[Set[str]]:
    """Return the set of MCP server names that should be stubbed.

    Reads the top-level ``mcp_servers`` config block (the real shape
    of the Hermes config — there is no ``mcp.servers`` sub-block).
    Returns ``None`` when the config has no ``mcp_servers`` at all,
    which the caller interprets as "stub every MCP tool regardless
    of server" (the safest default for the master-toggle case).
    """
    try:
        from hermes_cli.config import load_config  # noqa: PLC0415
        mcp_servers = load_config().get("mcp_servers", {}) or {}
    except Exception:
        return None
    if not mcp_servers:
        return None
    eligible: Set[str] = set()
    for name, spec in mcp_servers.items():
        if isinstance(spec, dict) and spec.get("lazy") is False:
            # Per-server explicit opt-out — log if description is set (M4).
            if isinstance(spec, dict) and spec.get("description"):
                logger.info(
                    "mcp_lazy: server '%s' has lazy=false; description config will be ignored",
                    name,
                )
            continue
        eligible.add(name)
    return eligible


def _server_descriptions() -> Dict[str, str]:
    """Return configured per-server descriptions (may be empty dict)."""
    try:
        from hermes_cli.config import load_config  # noqa: PLC0415
        mcp_servers = load_config().get("mcp_servers", {}) or {}
    except Exception:
        return {}
    result: Dict[str, str] = {}
    for name, spec in mcp_servers.items():
        if isinstance(spec, dict):
            desc = spec.get("description")
            if isinstance(desc, str) and desc.strip():
                result[name] = desc.strip()
    return result


def transform_tools(
    tools: List[Dict[str, Any]],
    agent: Any = None,
    **_kwargs: Any,
) -> Optional[List[Dict[str, Any]]]:
    """Return a tool list with un-promoted MCP tools replaced by stubs.

    Returns ``None`` when lazy loading is disabled, when the agent has
    no session_id, or on any internal error — letting the caller use
    the original ``tools`` list unchanged (fail-open).
    """
    try:
        cfg = _load_config()
        logger.info(
            "mcp_lazy: transform_tools called — lazy_loading=%r session_id=%r tool_count=%d",
            cfg.get("lazy_loading"),
            getattr(agent, "session_id", None) if agent is not None else None,
            len(tools),
        )
        mode = _lazy_mode(cfg)
        if mode == "off":
            logger.info("mcp_lazy: skipping — lazy_loading is off")
            return None  # master toggle off

        session_id = getattr(agent, "session_id", None) if agent is not None else None
        if not session_id:
            logger.info("mcp_lazy: skipping — no session_id on agent")
            return None

        pool = get_pool(session_id)
        # Anchor the pool on the agent so the registry's weak refs
        # don't lose it between calls.
        if agent is not None:
            try:
                setattr(agent, "_mcp_lazy_pool", pool)
            except Exception:
                pass

        # Stash the agent in a ContextVar so the load_mcp_tools
        # meta-tool handler can resolve it without registry.dispatch
        # having to plumb the agent reference explicitly. Tool
        # dispatch runs in the same task; contextvars propagate.
        try:
            _current_agent_var.set(agent)
        except Exception:
            pass

        pool.tick()

        eligible = _eligible_servers()

        if mode == "auto":
            # Pass through when the stub-eligible MCP schemas are too small
            # to be worth stubbing (Pattern 2). _lazy_active tells
            # pre_tool_call not to intercept calls on pass-through turns.
            candidate_cost = sum(
                _schema_tokens(t) for t in tools
                if t.get("function", {}).get("name", "").startswith("mcp_")
                and (eligible is None
                     or _server_in_set(t.get("function", {}).get("name", ""), eligible))
            )
            threshold = int(cfg.get("lazy_auto_threshold_tokens", 4000) or 4000)
            if candidate_cost < threshold:
                pool._lazy_active = False
                logger.info(
                    "mcp_lazy: auto mode pass-through — eligible MCP cost %d tok < threshold %d",
                    candidate_cost, threshold,
                )
                return None
        pool._lazy_active = True

        # Batched idle eviction (Pattern 3): only when the promoted set's
        # full-schema cost crosses the threshold, demote everything idle in
        # one sweep — one prompt-cache bust per batch, not per tool.
        idle_turns = int(cfg.get("lazy_evict_idle_turns", 10) or 0)
        if idle_turns > 0:
            promoted = pool.snapshot()
            if promoted:
                promoted_cost = sum(
                    _schema_tokens(t) for t in tools
                    if t.get("function", {}).get("name", "") in promoted
                )
                evict_threshold = int(
                    cfg.get("lazy_evict_cost_threshold_tokens", 3000) or 3000
                )
                if promoted_cost > evict_threshold:
                    evicted = pool.evict_idle(idle_turns)
                    if evicted:
                        logger.info(
                            "mcp_lazy: evicted %d idle promoted tool(s) "
                            "(promoted cost %d tok > %d): %s",
                            len(evicted), promoted_cost, evict_threshold,
                            ", ".join(sorted(evicted)),
                        )

        discovery_mode = str(cfg.get("discovery_mode", "tool") or "tool")
        if discovery_mode not in {"tool", "server", "both"}:
            logger.warning(
                "mcp_lazy: invalid discovery_mode=%r; falling back to 'tool'",
                discovery_mode,
            )
            discovery_mode = "tool"

        # Q11: detect mid-session mode flip and log WARNING.
        # _prev_mode is stored on the pool so it is cleared on session evict
        # (avoids the module-level dict leak fixed in #29).
        prev = pool._prev_mode
        if prev is not None and prev != discovery_mode:
            logger.warning(
                "mcp_lazy: discovery_mode changed mid-session %s: %r -> %r "
                "(promoted_servers state preserved in pool)",
                session_id, prev, discovery_mode,
            )
        pool._prev_mode = discovery_mode

        mcp_count = sum(1 for t in tools if t.get("function", {}).get("name", "").startswith("mcp_"))
        sample = next((t for t in tools if t.get("function", {}).get("name", "").startswith("mcp_")), None)
        if sample is None:
            sample = tools[0] if tools else None
        logger.info(
            "mcp_lazy: eligible_servers=%r discovery_mode=%r mcp_tools_detected=%d sample_keys=%r",
            eligible,
            discovery_mode,
            mcp_count,
            list(sample.keys()) if sample else None,
        )

        server_descs = _server_descriptions() if discovery_mode != "tool" else {}

        result = mix_full_and_stubs(
            tools,
            promoted_names=pool.snapshot(),
            lazy_servers=eligible,
            max_desc=int(cfg.get("lazy_stub_max_desc", 200) or 200),
            discovery_mode=discovery_mode,
            promoted_servers=pool.promoted_servers_snapshot(),
            server_descriptions=server_descs,
            server_stub_max_desc=int(cfg.get("server_stub_max_desc", 150) or 150),
        )
        stub_count = sum(1 for t in result if is_stub_schema(t))
        server_stub_count = sum(1 for t in result if is_server_stub_schema(t))
        full_mcp = sum(
            1 for t in result
            if t.get("function", {}).get("name", "").startswith("mcp_")
            and not is_stub_schema(t)
            and not is_server_stub_schema(t)
        )
        logger.info(
            "mcp_lazy: stubbed tool list — in=%d out=%d tool_stubs=%d server_stubs=%d full_mcp=%d",
            len(tools), len(result), stub_count, server_stub_count, full_mcp,
        )
        return result
    except Exception:
        logger.info("mcp_lazy: transform_tools EXCEPTION — returning None", exc_info=True)
        return None


def pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    session_id: str = "",
    **_kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Auto-promote a single tool if it's a stub and the agent is in lazy mode.

    Implements CRITICAL #1: when the model calls an MCP tool directly without
    loading its full schema first, promote the single tool and return a block
    directive so the model sees a "schema promoted; retry next turn" message
    instead of dispatching a stub-call that would error on schema validation.

    Returns ``{"action": "block", "message": "..."}`` to abort dispatch with
    the message surfaced as the tool's result.  Returns None to proceed with
    normal dispatch.

    Only fires when ``mcp.lazy_loading: true`` and the named tool is currently
    a stub (i.e. not yet promoted).  Does NOT auto-promote whole servers — per
    Q7, server promotion is explicit via ``load_mcp_server``.

    Reads agent from ``_current_agent_var`` ContextVar (set by
    ``transform_tools``); the pre_tool_call hook contract does not pass an
    agent kwarg.
    """
    try:
        cfg = _load_config()
        if _lazy_mode(cfg) == "off":
            return None
        if not tool_name or not tool_name.startswith("mcp_"):
            return None
        if not session_id:
            return None

        pool = get_pool(session_id)
        if not pool._lazy_active:
            # Auto-mode pass-through turn: full schemas were sent, no stubs
            # exist to intercept — blocking here would break legit calls.
            return None

        # Belt-and-suspenders for Interstellar-code/hermes-agent#18 and #27:
        # ``mcp_server_<name>`` *may* be a synthetic discovery stub, but a real
        # MCP server named "server" also produces tool names that start with
        # ``mcp_server_``.  We cannot rely on the name prefix alone — we must
        # check whether the tool is registered as a valid tool name on the agent
        # (concrete tools are in valid_tool_names; discovery stubs are NOT).
        # See #27 for the full collision analysis.
        if tool_name.startswith("mcp_server_"):
            agent = _current_agent_var.get(None)
            valid = getattr(agent, "valid_tool_names", None) or set()
            # If this name appears in valid_tool_names it IS a real MCP tool
            # (e.g. a tool from a server named "server"); fall through to the
            # normal per-tool auto-promote logic below.
            if valid and tool_name in valid:
                pass  # real concrete tool — fall through
            else:
                # This is a discovery stub.  Extract server name and handle
                # promoted vs unpromoted cases explicitly.
                server_name = tool_name[len("mcp_server_"):].strip()
                if server_name and pool.is_server_promoted(server_name):
                    prefix = f"mcp_{server_name}_"
                    concrete = sorted(t for t in valid if t.startswith(prefix))
                    hint = ", ".join(concrete[:8]) or "(use the mcp_{server}_<tool> names from the tool list)"
                    logger.info(
                        "mcp_lazy: pre_tool_call rejected stale server stub %r — server already promoted",
                        tool_name,
                    )
                    return {
                        "action": "block",
                        "message": (
                            f"[mcp_lazy] `{tool_name}` is a discovery stub for an already-promoted "
                            f"server. Call one of the concrete tools instead: {hint}"
                        ),
                    }
                # Server not yet promoted — block with a directive to use
                # load_mcp_server (fixes #31: was returning None, falling through
                # to dispatch which then errored on the stub).  Do NOT fall through
                # to per-tool auto-promote; stub names must never enter the pool.
                logger.info(
                    "mcp_lazy: pre_tool_call blocked unpromoted server stub %r — directing model to load_mcp_server",
                    tool_name,
                )
                return {
                    "action": "block",
                    "message": (
                        f"[mcp_lazy] `{tool_name}` is a server discovery stub, not a callable tool. "
                        "Call `load_mcp_server` with the server name to expand its tools, "
                        "then retry with a concrete tool."
                    ),
                }

        if pool.is_promoted(tool_name):
            pool.touch(tool_name)  # stamp use so idle eviction skips it
            return None  # already full schema — normal dispatch

        agent = _current_agent_var.get(None)
        if agent is None:
            return None  # no agent context — skip auto-promote

        # Check if this is a real MCP tool (not a hallucination).
        valid = getattr(agent, "valid_tool_names", None) or set()
        if valid and tool_name not in valid:
            return None  # unknown tool — let normal dispatch surface the error

        # Auto-promote the single tool (Q7: single tool, NOT whole server).
        from .promote import promote_tools  # noqa: PLC0415
        promote_tools(agent, [tool_name])
        logger.info(
            "mcp_lazy: pre_tool_call auto-promoted '%s'; blocking with next-turn retry (Q8)",
            tool_name,
        )
        return {
            "action": "block",
            "message": (
                f"[mcp_lazy] Tool `{tool_name}` was a stub — full schema promoted. "
                "Reissue the call on the next turn with proper parameters."
            ),
        }
    except Exception:
        logger.debug("mcp_lazy: pre_tool_call error", exc_info=True)
        return None


def on_session_reset(session_id: str = None, **_kwargs: Any) -> None:
    """Drop the previous session's pool.

    cli.py fires this AFTER agent.session_id has rotated to the new
    id — at this point any pool keyed on the old id is dead weight.
    We can't know the old id from kwargs, but the registry's
    WeakValueDictionary will GC it automatically once the agent's
    `_mcp_lazy_pool` attribute is overwritten on next ``transform_tools``
    call.

    This handler exists primarily as an explicit hook for future
    extensions (e.g. emitting a "session reset" event to a dashboard);
    the cleanup itself is GC-driven.
    """
    # Best-effort: if the caller passed an old_session_id (extension),
    # evict immediately.
    old_id = _kwargs.get("old_session_id")
    if isinstance(old_id, str) and old_id:
        evict(old_id)
