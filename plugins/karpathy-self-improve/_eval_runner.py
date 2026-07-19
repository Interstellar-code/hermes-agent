"""
_eval_runner.py — Scenario evaluation engine for karpathy-self-improve.

ScenarioRunner evaluates a list of binary check specs against a response text.
run_eval orchestrates loading scenarios, splitting train/holdout, running each
scenario through an injectable scenario_runner callable, evaluating checks
(including judge checks via an injectable judge_fn), and writing provenance rows.

GUARD: raises ValueError if proposer_model == judge_model (anti-gaming).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests  # type: ignore[import]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Check evaluation
# ---------------------------------------------------------------------------

CheckSpec = Dict[str, Any]
CheckResult = Tuple[bool, str]  # (passed, reason)


def evaluate_check(
    response_text: str,
    check: CheckSpec,
    *,
    judge_fn: Optional[Callable[[str, str], bool]] = None,
) -> CheckResult:
    """Evaluate a single check spec against *response_text*.

    Supported check types:
    - {"type": "must_contain", "value": "..."}
    - {"type": "must_not_contain", "value": "..."}
    - {"type": "max_tokens", "value": N}
    - {"type": "tool_used", "value": "name"}
    - {"type": "judge", "rubric": "..."}

    Returns (passed: bool, reason: str).
    """
    check_type = check.get("type", "")

    if check_type == "must_contain":
        value = str(check.get("value", ""))
        passed = value in response_text
        reason = f"contains {value!r}" if passed else f"missing {value!r}"
        return passed, reason

    if check_type == "must_not_contain":
        value = str(check.get("value", ""))
        passed = value not in response_text
        reason = f"absent {value!r}" if passed else f"unexpectedly contains {value!r}"
        return passed, reason

    if check_type == "max_tokens":
        limit = int(check.get("value", 0))
        # Rough approximation: split on whitespace for token count.
        token_count = len(response_text.split())
        passed = token_count <= limit
        reason = f"{token_count} tokens (limit {limit})" if passed else f"{token_count} tokens > limit {limit}"
        return passed, reason

    if check_type == "tool_used":
        tool_name = str(check.get("value", ""))
        # Check for tool use markers in the response text.
        passed = tool_name in response_text
        reason = f"tool {tool_name!r} used" if passed else f"tool {tool_name!r} not found in response"
        return passed, reason

    if check_type == "judge":
        rubric = str(check.get("rubric", ""))
        if judge_fn is None:
            return False, "judge check requires judge_fn but none provided"
        try:
            passed = bool(judge_fn(rubric, response_text))
            reason = "judge passed" if passed else "judge failed"
            return passed, reason
        except Exception as exc:  # pylint: disable=broad-except
            return False, f"judge_fn raised: {exc}"

    return False, f"unknown check type {check_type!r}"


def evaluate_checks(
    response_text: str,
    checks: List[CheckSpec],
    *,
    judge_fn: Optional[Callable[[str, str], bool]] = None,
) -> List[Dict[str, Any]]:
    """Evaluate all check specs against *response_text*.

    Returns list of dicts with keys: check (the original spec), passed (bool), reason (str).
    """
    results = []
    for check in checks:
        passed, reason = evaluate_check(response_text, check, judge_fn=judge_fn)
        results.append({"check": check, "passed": passed, "reason": reason})
    return results


# ---------------------------------------------------------------------------
# Default production scenario runner
# ---------------------------------------------------------------------------

from _wiring import GATEWAY_URL as _GATEWAY_CHAT_URL


def gateway_scenario_runner(scenario_input: str, *, model: Optional[str] = None) -> str:
    """POST scenario_input to the gateway chat API and return the response text.

    Only used in production — tests inject their own callable.
    """
    payload: Dict[str, Any] = {"message": scenario_input}
    if model:
        payload["model"] = model
    resp = requests.post(
        f"{_GATEWAY_CHAT_URL}/chat",
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    # Support both {"text": ...} and {"response": ...} shapes.
    return str(data.get("text") or data.get("response") or "")


# ---------------------------------------------------------------------------
# run_eval
# ---------------------------------------------------------------------------

def run_eval(
    db: Any,  # KarpathyDB
    experiment_id: int,
    profile: str,
    kind: str,
    *,
    scenario_runner: Optional[Callable[[str], str]] = None,
    judge_fn: Optional[Callable[[str, str], bool]] = None,
    proposer_model: Optional[str] = None,
    judge_model: Optional[str] = None,
    include_holdout: bool = False,
) -> float:
    """Run an eval pass for *experiment_id*.

    Args:
        db: KarpathyDB instance.
        experiment_id: The experiment to score against.
        profile: Profile name (used to load scenarios).
        kind: 'offline' or 'live'.
        scenario_runner: Callable(scenario_input) -> response_text. Defaults to
            gateway_scenario_runner (production only).
        judge_fn: Callable(rubric, response) -> bool. Required for judge checks.
        proposer_model: Model that proposed the change (stored for provenance).
        judge_model: Model used for judge checks (stored for provenance).
        include_holdout: If True, include holdout scenarios in the eval run
            (for final scoring). If False (default), only train scenarios are run.

    Returns:
        aggregate_score: Weighted pass rate across evaluated scenarios.

    Raises:
        ValueError: If proposer_model == judge_model (anti-gaming guard).
    """
    # Anti-gaming guard: both models must be explicitly set and must differ.
    if not proposer_model or not judge_model:
        raise ValueError(
            "proposer_model and judge_model must both be explicitly set."
        )
    if proposer_model == judge_model:
        raise ValueError(
            f"proposer_model and judge_model must differ; both are {proposer_model!r}. "
            "Using the same model as both proposer and judge defeats the evaluation."
        )

    if scenario_runner is None:
        scenario_runner = gateway_scenario_runner

    now = datetime.now(timezone.utc).isoformat()

    # Load scenarios for this profile.
    all_scenarios = db.list_scenarios(profile)

    # Split train vs holdout.
    train_scenarios = [s for s in all_scenarios if not s.get("holdout")]
    holdout_scenarios = [s for s in all_scenarios if s.get("holdout")]

    # Determine which scenarios to run.
    if include_holdout:
        scenarios_to_run = train_scenarios + holdout_scenarios
    else:
        scenarios_to_run = train_scenarios

    # Create the eval_run row.
    eval_run_id = db.insert_eval_run(
        experiment_id=experiment_id,
        kind=kind,
        proposer_model=proposer_model,
        judge_model=judge_model,
        aggregate_score=None,  # Will update after scoring.
        cost=None,
        created_at=now,
    )

    if not scenarios_to_run:
        logger.warning(
            "karpathy-self-improve: run_eval experiment=%d profile=%r has no scenarios to run",
            experiment_id,
            profile,
        )
        # Update with score=0.0 (no scenarios to evaluate).
        db.update_experiment_fields(experiment_id, updated_at=now)
        _update_eval_run_score(db, eval_run_id, 0.0)
        return 0.0

    pass_count = 0
    total_count = 0

    for scenario in scenarios_to_run:
        split = "holdout" if scenario.get("holdout") else "train"

        # Parse checks from JSON.
        raw_checks = scenario.get("checks", "[]")
        if isinstance(raw_checks, str):
            try:
                checks: List[CheckSpec] = json.loads(raw_checks)
            except json.JSONDecodeError:
                checks = []
        else:
            checks = list(raw_checks)

        scenario_input = scenario.get("input", "")

        # Run scenario through the runner.
        try:
            response_text = scenario_runner(scenario_input)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "karpathy-self-improve: scenario_runner failed for scenario %d: %s",
                scenario["id"],
                exc,
            )
            response_text = ""

        # Evaluate all checks.
        check_results = evaluate_checks(response_text, checks, judge_fn=judge_fn)

        # A scenario passes if all checks pass (or there are no checks).
        scenario_passed = all(r["passed"] for r in check_results) if check_results else True

        # Collect judge rationale from judge checks.
        judge_rationale_parts = [
            f"{r['check'].get('type','?')}: {r['reason']}"
            for r in check_results
            if r["check"].get("type") == "judge"
        ]
        judge_rationale = "; ".join(judge_rationale_parts)

        # Record provenance row.
        db.insert_scenario_result(
            eval_run_id=eval_run_id,
            scenario_id=scenario["id"],
            split=split,
            pass_fail=1 if scenario_passed else 0,
            judge_rationale=judge_rationale,
            scenario_snapshot={
                "profile": scenario.get("profile"),
                "name": scenario.get("name"),
                "input": scenario_input,
                "checks": checks,
            },
            created_at=now,
        )

        pass_count += 1 if scenario_passed else 0
        total_count += 1

    aggregate_score = pass_count / total_count if total_count > 0 else 1.0

    # Update the eval_run row with the final aggregate score.
    _update_eval_run_score(db, eval_run_id, aggregate_score)

    logger.debug(
        "karpathy-self-improve: run_eval experiment=%d kind=%r score=%.3f (%d/%d passed)",
        experiment_id,
        kind,
        aggregate_score,
        pass_count,
        total_count,
    )

    return aggregate_score


def _update_eval_run_score(db: Any, eval_run_id: int, score: float) -> None:
    """Update the aggregate_score on an eval_run row."""
    db._conn.execute(
        "UPDATE eval_runs SET aggregate_score = ? WHERE id = ?",
        (score, eval_run_id),
    )
    db._conn.commit()
