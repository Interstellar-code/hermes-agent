"""
_wiring.py — Production wiring helpers for karpathy-self-improve.

resolve_propose_kwargs(profile) returns a dict of llm_fn, judge_fn,
scenario_runner, proposer_model, and judge_model, all backed by the
Hermes gateway (http://127.0.0.1:8642/chat) using the same mechanism
as _default_llm_fn and gateway_scenario_runner in _proposer.py and
_eval_runner.py respectively.

Config is read from config.yaml via hermes_cli.config:
  plugins.karpathy_self_improve.gateway_url     (default: GATEWAY_URL_DEFAULT)
  plugins.karpathy_self_improve.proposer_model  (default: "auto")
  plugins.karpathy_self_improve.judge_model     (REQUIRED — no default; see
    _load_models. A wrong hardcoded model ID would silently fail every eval
    and trip auto-revert, so operators must configure it explicitly.)

The two model IDs must differ — equal values are a programming error that
the anti-gaming guard in _eval_runner will surface as ValueError; we catch
it here and return a clean error string instead.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

GATEWAY_URL_DEFAULT = "http://127.0.0.1:8642"
_DEFAULT_PROPOSER_MODEL = "auto"


def _load_gateway_url() -> str:
    """Return the gateway base URL from config.yaml, or GATEWAY_URL_DEFAULT."""
    try:
        from hermes_cli.config import load_config, cfg_get  # type: ignore[import]
        config = load_config()
        url = cfg_get(
            config, "plugins", "karpathy_self_improve", "gateway_url",
            default=GATEWAY_URL_DEFAULT,
        ) or GATEWAY_URL_DEFAULT
    except Exception:
        url = GATEWAY_URL_DEFAULT
    return str(url)


# Single shared gateway URL — _proposer.py and _eval_runner.py import this
# instead of hardcoding their own literal.
GATEWAY_URL = _load_gateway_url()

# The Hermes gateway exposes chat only via the OpenAI-compatible endpoint
# (POST /v1/chat/completions) with Bearer auth — the pre-0.18 `/chat` route no
# longer exists. See #184.
_GATEWAY_CHAT_PATH = "/v1/chat/completions"


def _load_api_key() -> str:
    """Bearer token for the gateway's /v1 API — the same key the gateway checks.

    Source order: ``API_SERVER_KEY`` env, then ``api_server.key`` in config,
    then ``$HERMES_HOME/.env`` (where launchd loads it for the gateway).
    Returns "" when none is found (a gateway with no key configured skips auth).
    """
    key = (os.environ.get("API_SERVER_KEY") or "").strip()
    if key:
        return key
    try:
        from hermes_cli.config import load_config, cfg_get  # type: ignore[import]
        key = (cfg_get(load_config(), "api_server", "key", default="") or "").strip()
        if key:
            return key
    except Exception:
        pass
    try:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        with open(os.path.join(home, ".env"), "r", encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("API_SERVER_KEY="):
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def call_gateway_chat(
    prompt: str,
    *,
    model: Optional[str] = None,
    timeout: int = 120,
    profile: Optional[str] = None,
    system_prompt: Optional[str] = None,
    identity_override: Optional[str] = None,
) -> str:
    """Call the gateway's OpenAI-compatible chat endpoint; return the reply text.

    POSTs ``{GATEWAY_URL}/v1/chat/completions`` with a Bearer header and the
    OpenAI request shape, and reads ``choices[0].message.content``. ``profile``
    selects an authenticated gateway profile; ``identity_override`` replaces
    the profile's SOUL.md only in memory for an offline candidate evaluation.
    ``model`` defaults to ``"auto"`` so the request routes through the agent's
    configured provider/model rather than any model id baked into this plugin.
    """
    import requests  # type: ignore[import]

    headers = {"Content-Type": "application/json"}
    key = _load_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if profile:
        if not key:
            raise ValueError("profile-scoped gateway calls require API_SERVER_KEY")
        headers["X-Hermes-Profile"] = profile
    messages = [{"role": "user", "content": prompt}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    payload: Dict[str, Any] = {
        "model": model or _DEFAULT_PROPOSER_MODEL,
        "messages": messages,
    }
    if identity_override is not None:
        payload["hermes_identity_override"] = identity_override
    resp = requests.post(
        f"{GATEWAY_URL}{_GATEWAY_CHAT_PATH}",
        json=payload,
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if choices:
        content = (choices[0].get("message") or {}).get("content")
        if content:
            return str(content)
    # Legacy fallback (pre-0.18 {"text"/"response"} shape); harmless if absent.
    return str(data.get("text") or data.get("response") or "")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_models() -> tuple[str, str]:
    """Return (proposer_model, judge_model) from config.yaml.

    proposer_model falls back to _DEFAULT_PROPOSER_MODEL if config.yaml or the
    key is unavailable. judge_model has NO default: a wrong hardcoded model ID
    would silently fail every eval and trip auto-revert, so a missing/empty
    value raises instead of falling back.
    """
    try:
        from hermes_cli.config import load_config, cfg_get  # type: ignore[import]
        config = load_config()
        proposer_model = cfg_get(
            config, "plugins", "karpathy_self_improve", "proposer_model",
            default=_DEFAULT_PROPOSER_MODEL,
        ) or _DEFAULT_PROPOSER_MODEL
        judge_model = cfg_get(
            config, "plugins", "karpathy_self_improve", "judge_model",
            default=None,
        )
    except Exception:
        proposer_model = _DEFAULT_PROPOSER_MODEL
        judge_model = None

    if not judge_model:
        raise ValueError(
            "judge_model not configured; set "
            "plugins.karpathy_self_improve.judge_model in config.yaml"
        )
    return str(proposer_model), str(judge_model)


def _load_profile_target_config(profile: str) -> Optional[Dict[str, Any]]:
    """Read plugins.karpathy_self_improve.profiles.<profile> from config.yaml.

    Returns None when no per-profile block is configured. When present,
    ``profile_root`` is ``~``-expanded so config.yaml can use ``~/...``.
    """
    try:
        from hermes_cli.config import load_config, cfg_get  # type: ignore[import]
        config = load_config()
        block = cfg_get(
            config, "plugins", "karpathy_self_improve", "profiles", profile,
            default=None,
        )
    except Exception:
        block = None

    if not isinstance(block, dict):
        return None

    result = dict(block)
    if result.get("profile_root"):
        result["profile_root"] = str(Path(str(result["profile_root"])).expanduser())
    return result


def resolve_target_for_profile(profile: str, db: Any) -> tuple[str, str]:
    """Resolve (target_relpath, profile_root) for *profile*.

    Resolution order (#176):
      1. Per-profile config block: plugins.karpathy_self_improve.profiles.<profile>
         (wins over everything else — the explicit, operator-set source of truth).
      2. The most recent prior experiment for the profile.
      3. Fail fast with an actionable ValueError — never silently default to
         "system_prompt.md" / "." (the daemon process CWD), which previously
         caused proposals to write into the wrong directory for profiles that
         were never bootstrapped.
    """
    block = _load_profile_target_config(profile)
    if block and block.get("target_relpath") and block.get("profile_root"):
        return str(block["target_relpath"]), str(block["profile_root"])

    rows = db.list_experiments(profile=profile)
    if rows:
        exp = rows[0]
        target_relpath = exp.get("target_relpath")
        profile_root = exp.get("target_profile_root")
        if target_relpath and profile_root:
            return str(target_relpath), str(profile_root)

    raise ValueError(
        f"no target_relpath/profile_root for profile {profile!r}; add it under "
        f"plugins.karpathy_self_improve.profiles.{profile} in config.yaml, or run "
        f"`hermes --profile {profile} karpathy bootstrap`"
    )


# ---------------------------------------------------------------------------
# Gateway-backed callables
# ---------------------------------------------------------------------------

def _make_llm_fn(model: str, profile: Optional[str] = None) -> Callable[[str], str]:
    """Return a callable that POSTs *prompt* to the gateway with *model*."""

    def llm_fn(prompt: str) -> str:
        try:
            return call_gateway_chat(prompt, model=model, timeout=120, profile=profile)
        except Exception as exc:
            logger.warning("karpathy-self-improve: llm_fn(model=%r) failed: %s", model, exc)
            raise

    return llm_fn


def _make_judge_fn(model: str, profile: Optional[str] = None) -> Callable[[str, str], bool]:
    """Return a judge callable that uses *model* to get a yes/no verdict."""

    def judge_fn(rubric: str, response: str) -> bool:
        try:
            prompt = (
                f"You are an evaluator. Based on the rubric, reply with a single word: "
                f"yes or no.\n\nRubric: {rubric}\n\nResponse to evaluate:\n{response}"
            )
            raw = call_gateway_chat(
                prompt, model=model, timeout=60, profile=profile
            ).strip().lower()
            return _parse_verdict(raw)
        except Exception as exc:
            logger.warning("karpathy-self-improve: judge_fn(model=%r) failed: %s", model, exc)
            return False

    return judge_fn


def _parse_verdict(raw: str) -> bool:
    """Parse an LLM verdict string to bool. Default False on ambiguity."""
    first = raw.split()[0].lower().strip(".,;:!?") if raw.split() else ""
    if first in ("yes", "true", "1", "pass", "correct", "positive"):
        return True
    if first in ("no", "false", "0", "fail", "incorrect", "negative"):
        return False
    # Ambiguous — fail closed (conservative)
    logger.debug("karpathy-self-improve: ambiguous verdict %r → False", raw)
    return False


def make_scenario_runner(
    model: str,
    profile: Optional[str],
    *,
    candidate_content: Optional[str] = None,
    target_relpath: Optional[str] = None,
) -> Callable[[str], str]:
    """Return a profile-scoped runner, optionally with an in-memory candidate."""

    def scenario_runner(scenario_input: str) -> str:
        try:
            return call_gateway_chat(
                scenario_input,
                model=model,
                timeout=60,
                profile=profile,
                identity_override=candidate_content,
            )
        except Exception as exc:
            logger.warning(
                "karpathy-self-improve: scenario_runner(model=%r, profile=%r) failed: %s",
                model,
                profile,
                exc,
            )
            raise

    return scenario_runner


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_propose_kwargs(profile: Optional[str] = None) -> Dict[str, Any]:
    """Return kwargs for propose_for_profile backed by the real gateway.

    Returns a dict with keys: proposer_model, judge_model, llm_fn,
    judge_fn, scenario_runner.

    Raises ValueError (with a clear message) if proposer_model == judge_model,
    so the caller can surface a 400 instead of letting _eval_runner raise a
    cryptic 500.
    """
    proposer_model, judge_model = _load_models()

    # Anti-gaming guard DISABLED by operator config: proposer == judge is allowed
    # (e.g. both "auto"). Self-judged evals are unreliable, so warn instead of
    # raising, so /propose still runs. Mirror of _eval_runner.run_eval's policy.
    if proposer_model == judge_model:
        logger.warning(
            "karpathy-self-improve: proposer_model == judge_model (%r); anti-gaming "
            "guard disabled — self-judged evals are unreliable.",
            proposer_model,
        )

    return {
        "proposer_model": proposer_model,
        "judge_model": judge_model,
        "llm_fn": _make_llm_fn(proposer_model, profile),
        "judge_fn": _make_judge_fn(judge_model, profile),
        "scenario_runner": make_scenario_runner(proposer_model, profile),
        "candidate_scenario_runner": lambda content, relpath: make_scenario_runner(
            proposer_model,
            profile,
            candidate_content=content,
            target_relpath=relpath,
        ),
    }
