"""system-one-router — advisory routing decisions via the hosted Jev decision API.

v1 scope (plan §12.6): ADVISORY ONLY. Jev suggests (kind + lane + confidence);
Switch still decides. Fail-closed everywhere; dormant until plugins.system-one-router.enabled.

Design rules honored (see plugin-authoring skill):
- register() never probes the network and never raises
- per-tool check_fn gates are CHEAP (config + key presence only) and never
  touch the network: the registry TTL-caches check_fn results for 30s, so the
  gate runs roughly every 30s at worst. The paid reachability signal is
  observed per-call via the fallback reasons on tool results instead.
- load_config() really reads config.yaml overlay
- no dashboard API, no kanban audit mirror

Spend cap semantics (best-effort, by design): the monthly cap is enforced as
check-then-call-then-record, which is NOT atomic across concurrent callers —
the paid network call necessarily happens between the check and the record of
its own cost, and holding a SQLite lock across a network call would be worse
 than the small overshoot. Concurrent calls can each pass the cap check before
any of their costs is recorded; the cap therefore bounds spend to roughly
max_monthly_usd + (one in-flight call per concurrent caller). The cheap gate
keeps call volume low, and every spent call (including lane follow-ups and
fallbacks that still cost tokens) is recorded to the decision log.
"""
from __future__ import annotations

import json
import math

from ._client import (DEFAULT_BASE_URL, DEFAULT_MODEL, FALLBACK_REASONS, JevClient,
                      _NoRedirectHandler, _build_no_redirect_opener, read_api_key)
from ._config import DEFAULTS, load_config
from ._decisionlog import DecisionLog, hash_state


def _config_defaults() -> dict:
    """Copy of the plugin's config DEFAULTS (test/operator seam)."""
    return dict(DEFAULTS)

_ROUTING_QUESTIONS = {
    "kind": {
        "type": "choice",
        "instructions": (
            "Classify the CURRENT message by its nature. A fresh_task is a NEW, self-contained "
            "request for hands-on work that could be handed to a specialist as a standalone "
            "assignment. A continuation is a reply, confirmation, correction, or steering input "
            "inside an exchange already in progress. general is chat, status, questions, or "
            "anything needing no specialist work."
        ),
        "criteria": {
            "fresh_task": {
                "what": "A new actionable work request, stated so it makes sense on its own",
                "not_for": "Short replies like 'yes', 'do that', quoted follow-ups, status checks",
                "examples": ["fix the failing auth test", "deploy v2 to production", "audit this invoice"],
            },
            "continuation": {
                "what": "A reply that continues, confirms, or steers ongoing work",
                "not_for": "A brand-new assignment that opens a new piece of work",
                "examples": ["yes pls", "go ahead with option 2", "> [Re: #12] do that"],
            },
            "general": {
                "what": "Conversation, questions, status, reminders — no hands-on work requested",
                "not_for": "Any message requesting concrete work be done",
                "examples": ["what's the status", "thanks", "what do you think about X"],
            },
        },
    },
    "lane": {
        "type": "choice",
        "instructions": (
            "IF this message is a fresh_task: which specialist lane should execute it? "
            "neo = hands-on code/infra/deploy/test work. morpheus = design/architecture/content. "
            "trinity = invoices/budgets/vendors/finance. switch = anything else."
        ),
        "criteria": {
            "neo": {"what": "Writing/fixing code, bugs, infra, deployments, repo ops, running tests",
                    "not_for": "Discussions about code, or non-code work",
                    "examples": ["fix this failing test", "deploy the new version"]},
            "morpheus": {"what": "Design, architecture review, marketing, brand, content strategy",
                         "not_for": "Implementing a design in code",
                         "examples": ["review this architecture", "draft the launch post"]},
            "trinity": {"what": "Invoices, budgets, costs, vendors, compliance, financial reconciliation",
                        "not_for": "Technical work that merely mentions prices",
                        "examples": ["audit this invoice", "reconcile the vendor bill"]},
            "switch": {"what": "Any fresh task that is none of the above; the orchestrator handles it",
                       "not_for": "Clearly-scoped code/design/finance work",
                       "examples": ["research topic X and report back"]},
        },
    },
}


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _cap_usd(cfg: dict) -> float:
    """Monthly cap; non-finite/negative/garbage -> 0.0 (fail closed: cap always trips)."""
    v = _to_float(cfg.get("max_monthly_usd"))
    return v if math.isfinite(v) and v >= 0 else 0.0


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _merge_usage(a: dict | None, b: dict | None) -> dict:
    """Sum two usage dicts into a fresh dict (missing/None/bad values count as 0)."""
    ua = a if isinstance(a, dict) else {}
    ub = b if isinstance(b, dict) else {}
    return {
        "input_tokens": _to_int(ua.get("input_tokens")) + _to_int(ub.get("input_tokens")),
        "output_tokens": _to_int(ua.get("output_tokens")) + _to_int(ub.get("output_tokens")),
        "cost_usd": _to_float(ua.get("cost_usd")) + _to_float(ub.get("cost_usd")),
    }


def _tool_gate() -> bool:
    """Cheap check_fn gate for the paid routing tools: enabled + key present.

    NEVER touches the network. The registry TTL-caches this for ~30s. Actual
    reachability shows up per-call as a fallback reason on the tool result.
    """
    try:
        cfg = load_config()
        if not cfg.get("enabled"):
            return False
        return bool(read_api_key())
    except Exception:
        return False


def _status_handler(args: dict, **_injected):
    """Diagnostic: config + key presence + decision-log stats. Registered with
    the cheap check_fn gate (finding 5: no paid probe, no network, no spend —
    reachability is observed per-call via the fallback reasons on the paid
    tools' results)."""
    try:
        cfg = load_config()
        key_present = bool(read_api_key())
        log = DecisionLog()
        return {
            "enabled": bool(cfg.get("enabled")),
            "key_present": key_present,
            "model": cfg.get("model"),
            "max_monthly_usd": cfg.get("max_monthly_usd"),
            "log": log.stats(),
            "reachability": ("not probed here; observed per-call via the fallback"
                             " reasons on system_one_route/system_one_decide results"),
            "note": "advisory-only v1: Jev suggests, Switch decides",
        }
    except Exception:
        return {"enabled": False, "key_present": False,
                "reason": "status_unavailable",
                "note": "advisory-only v1: Jev suggests, Switch decides"}


def _build_state(args: dict) -> dict:
    # Text leaves the process to a third party: scrub secrets first (fails closed in redact_for_egress).
    from agent.redact import redact_for_egress as _r

    state = {
        "message": _r(str(args.get("message", "")))[:1200],
        "is_followup": bool(args.get("is_followup", False)),
    }
    if args.get("reply_target"):
        state["reply_target"] = _r(str(args["reply_target"]))[:400]
    conversation = args.get("conversation")
    if isinstance(conversation, list):
        # Finding 12: filter non-dict entries FIRST, then keep the most
        # recent four ([-4:], not the oldest four).
        cleaned = [
            {"from": _r(str(c.get("from", "user")))[:20], "text": _r(str(c.get("text", "")))[:400]}
            for c in conversation if isinstance(c, dict)
        ][-4:]
        if cleaned:
            state["conversation"] = cleaned
    return state


def _route_handler(args: dict, **_injected):
    try:
        cfg = load_config()
        if not cfg.get("enabled"):
            return {"status": "fallback", "reason": "disabled"}
        client = JevClient(api_key=read_api_key(),
                           base_url=str(cfg.get("base_url") or DEFAULT_BASE_URL),
                           model=str(cfg.get("model") or DEFAULT_MODEL))
        log = DecisionLog()
        state = _build_state(args if isinstance(args, dict) else {})
        state_hash = hash_state(state)
        # Not logged: a zero-cost row per call after the cap trips would grow the log unbounded.
        if log.month_spend_usd() >= _cap_usd(cfg):
            return {"status": "fallback", "reason": "cap_exceeded"}

        res = client.decide(state, _ROUTING_QUESTIONS, question_key="kind") or {}
        usage = _merge_usage(res.get("usage"), None)
        kind = res.get("choice")
        lane = None
        confidence = res.get("confidence")
        latency_ms = _to_int(res.get("latency_ms"))
        if kind == "fresh_task" and res.get("status") == "ok":
            lane_res = client.decide(state, {"lane": _ROUTING_QUESTIONS["lane"]},
                                     question_key="lane") or {}
            usage = _merge_usage(usage, lane_res.get("usage"))
            latency_ms += _to_int(lane_res.get("latency_ms"))
            if lane_res.get("status") == "ok":
                lane = lane_res.get("choice")
                confidence = min(_to_float(confidence), _to_float(lane_res.get("confidence")))
            else:
                # The lane call still cost money — record it, then propagate the
                # lane fallback instead of masquerading as a fresh ok result.
                log.record(state_hash, "route", str(kind), None,
                           str(lane_res.get("status") or "fallback"), lane_res.get("reason"),
                           latency_ms, usage["cost_usd"])
                return {
                    "status": lane_res.get("status") or "fallback",
                    "reason": lane_res.get("reason"),
                    "kind": kind,
                    "lane": None,
                    "confidence": None,
                    "latency_ms": latency_ms,
                    "usage": usage,
                    "advisory": True,
                }
        status = res.get("status") or "fallback"
        reason = res.get("reason")
        log.record(state_hash, "route", f"{kind}:{lane}" if lane else kind,
                   _conf_or_none(confidence), status, reason, latency_ms,
                   usage["cost_usd"])
        return {
            "status": status,
            "reason": reason,
            "kind": kind,
            "lane": lane,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "usage": usage,
            "advisory": True,
        }
    except Exception:
        return {"status": "fallback", "reason": "handler_error", "advisory": True}


def _conf_or_none(value) -> float | None:
    """Coerce a confidence to float or None — never store a raw string."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


_MAX_STATE_JSON_CHARS = 8192
_MAX_QUESTIONS = 3
_MAX_QUESTION_JSON_CHARS = 4096


def _decide_handler(args: dict, **_injected):
    try:
        cfg = load_config()
        if not cfg.get("enabled"):
            return {"status": "fallback", "reason": "disabled"}
        client = JevClient(api_key=read_api_key(),
                           base_url=str(cfg.get("base_url") or DEFAULT_BASE_URL),
                           model=str(cfg.get("model") or DEFAULT_MODEL))
        log = DecisionLog()
        state = args.get("state")
        questions = args.get("questions")
        if not state or not isinstance(questions, dict):
            return {"status": "fallback", "reason": "bad_payload"}

        # Input caps (finding 11): bound the model input BEFORE the paid call.
        # Choice asserted: REJECT (fail-closed), never silently truncate.
        try:
            state_json = state if isinstance(state, str) else json.dumps(state, default=str)
        except Exception:
            return {"status": "fallback", "reason": "bad_payload"}
        if len(state_json) > _MAX_STATE_JSON_CHARS or len(questions) > _MAX_QUESTIONS:
            return {"status": "fallback", "reason": "payload_too_large"}
        for q_val in questions.values():
            try:
                q_json = q_val if isinstance(q_val, str) else json.dumps(q_val, default=str)
            except Exception:
                return {"status": "fallback", "reason": "bad_payload"}
            if len(q_json) > _MAX_QUESTION_JSON_CHARS:
                return {"status": "fallback", "reason": "payload_too_large"}

        # The hash must cover BOTH inputs, not just state: same state asked a
        # different question is a different decision.
        state_hash = hash_state({"state": state, "questions": questions})
        if log.month_spend_usd() >= _cap_usd(cfg):
            return {"status": "fallback", "reason": "cap_exceeded"}
        key = next(iter(questions), "q")
        res = client.decide(state, questions, question_key=key) or {}
        usage = _merge_usage(res.get("usage"), None)
        latency_ms = _to_int(res.get("latency_ms"))
        status = res.get("status") or "fallback"
        log.record(state_hash, str(key), res.get("choice"), _conf_or_none(res.get("confidence")),
                   status, res.get("reason"), latency_ms, usage["cost_usd"])
        return {k: res.get(k) for k in ("status", "reason", "choice", "confidence", "latency_ms")}
    except Exception:
        return {"status": "fallback", "reason": "handler_error"}


def register(ctx) -> None:
    try:
        ctx.register_tool(
            name="system_one_status",
            toolset="system_one_router",
            schema={"type": "object", "properties": {}},
            handler=_status_handler,
            check_fn=_tool_gate,
            is_async=False,
            description=("Diagnostic: system-one-router config, key presence, decision-log stats."
                         " Cheap; no network probe — reachability is observed per-call via fallback reasons."),
            emoji="🧭",
        )
        ctx.register_tool(
            name="system_one_route",
            toolset="system_one_router",
            schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "conversation": {"type": "array",
                                     "items": {"type": "object",
                                               "properties": {"from": {"type": "string"},
                                                              "text": {"type": "string"}}}},
                    "is_followup": {"type": "boolean"},
                    "reply_target": {"type": "string"},
                },
                "required": ["message"],
            },
            handler=_route_handler,
            check_fn=_tool_gate,
            is_async=False,
            description="Advisory routing suggestion (kind + lane + confidence) via the Jev decision API. Suggestion only — Switch decides.",
            emoji="🧭",
        )
        ctx.register_tool(
            name="system_one_decide",
            toolset="system_one_router",
            schema={
                "type": "object",
                "properties": {
                    "state": {},
                    "questions": {"type": "object"},
                },
                "required": ["state", "questions"],
            },
            handler=_decide_handler,
            check_fn=_tool_gate,
            is_async=False,
            description="Generic typed decision (choice/score/noul) via the Jev decision API. Advisory; fail-closed.",
            emoji="🧭",
        )
        from pathlib import Path

        skill_path = Path(__file__).parent / "skills" / "system-one-routing" / "SKILL.md"
        if skill_path.exists():
            ctx.register_skill(
                name="system-one-routing",
                path=skill_path,
                description="Discipline for using system-one-router: advisory-only, confidence gates, fail-closed fallback.",
            )
    except Exception:
        # register() must never raise; a broken plugin load must not take the agent down.
        pass
