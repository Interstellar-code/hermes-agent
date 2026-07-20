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
_GATEWAY_CHAT_URL = f"{GATEWAY_URL}/chat"


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

def _make_llm_fn(model: str) -> Callable[[str], str]:
    """Return a callable that POSTs *prompt* to the gateway with *model*."""

    def llm_fn(prompt: str) -> str:
        try:
            import requests  # type: ignore[import]
            payload: Dict[str, Any] = {"message": prompt, "model": model}
            resp = requests.post(_GATEWAY_CHAT_URL, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            return str(data.get("text") or data.get("response") or "")
        except Exception as exc:
            logger.warning("karpathy-self-improve: llm_fn(model=%r) failed: %s", model, exc)
            raise

    return llm_fn


def _make_judge_fn(model: str) -> Callable[[str, str], bool]:
    """Return a judge callable that uses *model* to get a yes/no verdict."""

    def judge_fn(rubric: str, response: str) -> bool:
        try:
            import requests  # type: ignore[import]
            prompt = (
                f"You are an evaluator. Based on the rubric, reply with a single word: "
                f"yes or no.\n\nRubric: {rubric}\n\nResponse to evaluate:\n{response}"
            )
            payload: Dict[str, Any] = {"message": prompt, "model": model}
            resp = requests.post(_GATEWAY_CHAT_URL, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            raw = str(data.get("text") or data.get("response") or "").strip().lower()
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


def _make_scenario_runner(model: str) -> Callable[[str], str]:
    """Return a scenario runner that sends input through the gateway with *model*."""

    def scenario_runner(scenario_input: str) -> str:
        try:
            import requests  # type: ignore[import]
            payload: Dict[str, Any] = {"message": scenario_input, "model": model}
            resp = requests.post(_GATEWAY_CHAT_URL, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            return str(data.get("text") or data.get("response") or "")
        except Exception as exc:
            logger.warning(
                "karpathy-self-improve: scenario_runner(model=%r) failed: %s", model, exc
            )
            raise

    return scenario_runner


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_propose_kwargs(profile: Optional[str] = None) -> Dict[str, Any]:  # noqa: ARG001
    """Return kwargs for propose_for_profile backed by the real gateway.

    Returns a dict with keys: proposer_model, judge_model, llm_fn,
    judge_fn, scenario_runner.

    Raises ValueError (with a clear message) if proposer_model == judge_model,
    so the caller can surface a 400 instead of letting _eval_runner raise a
    cryptic 500.
    """
    proposer_model, judge_model = _load_models()

    if proposer_model == judge_model:
        raise ValueError(
            f"proposer_model and judge_model must differ; both are {proposer_model!r}. "
            "Set distinct values under plugins.karpathy_self_improve in config.yaml."
        )

    return {
        "proposer_model": proposer_model,
        "judge_model": judge_model,
        "llm_fn": _make_llm_fn(proposer_model),
        "judge_fn": _make_judge_fn(judge_model),
        "scenario_runner": _make_scenario_runner(proposer_model),
    }
