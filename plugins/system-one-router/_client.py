"""Typed client for the hosted Jev decision API (fail-closed, zero retries).

Verified live 2026-09-28/29 (Phase 1 experiments):
- Endpoint: https://jevtypesafeai.com/api/v1/decide  (api.typesafe.ai 401s our key)
- Bearer auth; key read from ~/.hermes/.env JEV_API_KEY at runtime
- Choice questions REQUIRE instructions + criteria (NOT "options" — 400s)
- Response: {"answers": {<key>: {type, choice|score|noul, confidence, probabilities}},
             "usage": {input_tokens, output_tokens, cost_usd, credits_remaining_usd}}
Every failure returns {"status": "fallback", "reason": <enum>} — never raises.
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://jevtypesafeai.com/api/v1/decide"
# Base-url host allowlist (finding 10): the Bearer key is sent to whatever
# base_url the config/env names, so a poisoned config could otherwise exfil
# the key to an arbitrary host. Exact-host match, subdomains NOT implied.
ALLOWED_BASE_HOSTS = frozenset({"jevtypesafeai.com", "api.typesafe.ai"})
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_S = 5.0
RETRY_COUNT = 0  # a failed decision falls back immediately; never pay twice

FALLBACK_REASONS = (
    "missing_api_key", "timeout", "unreachable", "http_429", "http_error",
    "bad_payload", "cap_exceeded", "insecure_url", "payload_too_large",
)


def _strip_key_value(raw: str) -> str:
    """Strip one matched pair of surrounding quotes from a .env value."""
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def read_api_key(env_path: Path | None = None) -> str:
    """Key resolution order: (1) JEV_API_KEY env var, (2) profile .env.

    The .env lookup resolves the profile home via hermes_constants.get_hermes_home
    (lazy import so the plugin loads outside a Hermes runtime), falling back to
    ~/.hermes/.env. A leading 'export ' prefix and one matched pair of
    surrounding quotes are stripped from the file value.
    """
    env_val = os.environ.get("JEV_API_KEY")
    if env_val is not None and env_val.strip():
        return env_val.strip()
    if env_path is None:
        try:
            from hermes_constants import get_hermes_home

            env_path = Path(get_hermes_home()) / ".env"
        except Exception:
            env_path = Path.home() / ".hermes" / ".env"
    try:
        for ln in env_path.read_text().splitlines():
            ln = ln.strip()
            if ln.startswith("export "):
                ln = ln[len("export "):].lstrip()
            if ln.startswith("JEV_API_KEY="):
                return _strip_key_value(ln.split("=", 1)[1])
    except OSError:
        pass
    return ""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a 3xx raises HTTPError instead of being followed,
    so the Authorization: Bearer header can never be re-sent to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _build_no_redirect_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirectHandler())


_OPENER: urllib.request.OpenerDirector | None = None


def _get_opener() -> urllib.request.OpenerDirector:
    """Process-lazy module-level opener (built once, no network at import)."""
    global _OPENER
    if _OPENER is None:
        _OPENER = _build_no_redirect_opener()
    return _OPENER


def _send_request(req: urllib.request.Request, timeout: float):
    """The urlopen-equivalent seam: sends via the no-redirect opener. Tests
    patch this instead of urllib.request.urlopen so the redirect refusal stays
    part of the exercised path."""
    return _get_opener().open(req, timeout=timeout)


class JevClient:
    def __init__(self, api_key: str = "", base_url: str = DEFAULT_BASE_URL,
                 model: str = DEFAULT_MODEL, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout_s = timeout_s

    def decide(self, state: Any, questions: dict, question_key: str | None = None) -> dict:
        """One decision call. Returns:
        ok:       {"status":"ok", "answers": {...}, "choice": <for question_key>, "confidence": ...,
                   "latency_ms": int, "usage": {...}}
        fallback: {"status":"fallback","reason":<one of FALLBACK_REASONS>, choice/confidence None}
        """
        t0 = time.monotonic()
        key = question_key or (next(iter(questions)) if questions else "q")

        def fallback(reason: str) -> dict:
            return {
                "status": "fallback", "reason": reason,
                "choice": None, "confidence": None, "answers": {},
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
            }

        if not self.api_key:
            return fallback("missing_api_key")

        parsed = urllib.parse.urlsplit(str(self.base_url))
        # SSRF/exfil hardening (finding 10): https:// required AND the host
        # must be on the allowlist — never send the Bearer key anywhere else.
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_BASE_HOSTS:
            return fallback("insecure_url")

        state_str = state if isinstance(state, str) else json.dumps(state, default=str)
        payload = {"model": self.model, "state": state_str, "questions": questions}
        try:
            body = json.dumps(payload).encode()
        except Exception:
            return fallback("bad_payload")

        req = urllib.request.Request(
            self.base_url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        for _attempt in range(RETRY_COUNT + 1):  # exactly one iteration
            try:
                opener_open = _send_request(req, timeout=self.timeout_s)
                with opener_open as resp:
                    data = json.loads(resp.read().decode())
                # Fail-closed payload validation (finding 13): anything that is
                # not a dict, lacks 'answers', or carries a non-numeric
                # cost_usd is a bad payload — never an AttributeError swallowed
                # into a wrong "ok".
                if not isinstance(data, dict):
                    return fallback("bad_payload")
                answers = data.get("answers")
                if not isinstance(answers, dict):
                    return fallback("bad_payload")
                ans = answers.get(key) or {}
                usage = data.get("usage") or {}
                cost_usd = float(usage["cost_usd"])  # KeyError/TypeError/ValueError → bad_payload
                return {
                    "status": "ok",
                    "answers": answers,
                    "choice": ans.get("choice"),
                    "noul": ans.get("noul"),
                    "score": ans.get("score"),
                    "confidence": ans.get("confidence"),
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "usage": {
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "cost_usd": cost_usd,
                        "credits_remaining_usd": usage.get("credits_remaining_usd"),
                    },
                }
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    return fallback("http_429")
                return fallback("http_error")
            except urllib.error.URLError as e:
                reason = getattr(e, "reason", None)
                if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason or "").lower():
                    return fallback("timeout")
                return fallback("unreachable")
            except (TimeoutError, socket.timeout):
                # Bare socket.timeout during resp.read() (finding 14) —
                # previously swallowed into bad_payload.
                return fallback("timeout")
            except Exception:
                return fallback("bad_payload")
        return fallback("http_error")
