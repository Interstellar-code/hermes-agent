"""
test_eval_runner.py — Tests for _eval_runner.py.

Covers:
- evaluate_check for each check type (must_contain, must_not_contain,
  max_tokens, tool_used, judge)
- evaluate_checks returns correct structure
- run_eval writes eval_runs + experiment_scenario_results rows with correct split
- holdout scenarios excluded by default, included when include_holdout=True
- proposer_model == judge_model raises ValueError
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# DB fixture (matches test_db.py pattern)
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test-karpathy.db")
    monkeypatch.setenv("KARPATHY_DB_PATH", db_file)

    import _db as db_mod
    monkeypatch.setattr(db_mod, "_conn", None)

    from _db import open_db
    return open_db(Path(db_file))


# ---------------------------------------------------------------------------
# evaluate_check
# ---------------------------------------------------------------------------

class TestEvaluateCheck:
    def test_must_contain_pass(self):
        from _eval_runner import evaluate_check
        passed, reason = evaluate_check("hello world", {"type": "must_contain", "value": "hello"})
        assert passed is True
        assert "hello" in reason

    def test_must_contain_fail(self):
        from _eval_runner import evaluate_check
        passed, reason = evaluate_check("hello world", {"type": "must_contain", "value": "missing"})
        assert passed is False

    def test_must_not_contain_pass(self):
        from _eval_runner import evaluate_check
        passed, reason = evaluate_check("hello world", {"type": "must_not_contain", "value": "bad"})
        assert passed is True

    def test_must_not_contain_fail(self):
        from _eval_runner import evaluate_check
        passed, reason = evaluate_check("bad content here", {"type": "must_not_contain", "value": "bad"})
        assert passed is False

    def test_max_tokens_pass(self):
        from _eval_runner import evaluate_check
        # "one two three" is 3 tokens (words)
        passed, reason = evaluate_check("one two three", {"type": "max_tokens", "value": 10})
        assert passed is True

    def test_max_tokens_fail(self):
        from _eval_runner import evaluate_check
        # 5 words, limit 3
        passed, reason = evaluate_check("one two three four five", {"type": "max_tokens", "value": 3})
        assert passed is False

    def test_tool_used_pass(self):
        from _eval_runner import evaluate_check
        passed, reason = evaluate_check(
            "used search_tool here",
            {"type": "tool_used", "value": "search_tool"},
        )
        assert passed is True
        assert "search_tool" in reason

    def test_tool_used_fail(self):
        from _eval_runner import evaluate_check
        passed, reason = evaluate_check(
            "no tools mentioned",
            {"type": "tool_used", "value": "search_tool"},
        )
        assert passed is False

    def test_judge_pass(self):
        from _eval_runner import evaluate_check
        judge_fn = lambda rubric, response: True
        passed, reason = evaluate_check(
            "some response",
            {"type": "judge", "rubric": "Is it good?"},
            judge_fn=judge_fn,
        )
        assert passed is True

    def test_judge_fail(self):
        from _eval_runner import evaluate_check
        judge_fn = lambda rubric, response: False
        passed, reason = evaluate_check(
            "some response",
            {"type": "judge", "rubric": "Is it good?"},
            judge_fn=judge_fn,
        )
        assert passed is False

    def test_judge_no_judge_fn_returns_false(self):
        from _eval_runner import evaluate_check
        passed, reason = evaluate_check(
            "some response",
            {"type": "judge", "rubric": "rubric"},
            judge_fn=None,
        )
        assert passed is False

    def test_judge_fn_exception_returns_false(self):
        from _eval_runner import evaluate_check
        def bad_judge(rubric, response):
            raise RuntimeError("exploded")
        passed, reason = evaluate_check(
            "response",
            {"type": "judge", "rubric": "r"},
            judge_fn=bad_judge,
        )
        assert passed is False
        assert "raised" in reason

    def test_unknown_type_returns_false(self):
        from _eval_runner import evaluate_check
        passed, reason = evaluate_check("text", {"type": "no_such_type"})
        assert passed is False
        assert "unknown" in reason


# ---------------------------------------------------------------------------
# evaluate_checks
# ---------------------------------------------------------------------------

def test_evaluate_checks_returns_list_with_structure():
    from _eval_runner import evaluate_checks
    checks = [
        {"type": "must_contain", "value": "hello"},
        {"type": "must_not_contain", "value": "bad"},
    ]
    results = evaluate_checks("hello world", checks)
    assert len(results) == 2
    for r in results:
        assert "check" in r
        assert "passed" in r
        assert "reason" in r


def test_evaluate_checks_empty():
    from _eval_runner import evaluate_checks
    assert evaluate_checks("text", []) == []


# ---------------------------------------------------------------------------
# run_eval
# ---------------------------------------------------------------------------

def _make_experiment(db, profile="p"):
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    return db.insert_experiment(
        profile=profile, state="proposed", created_at=ts, updated_at=ts
    )


def _make_scenario(db, profile="p", *, holdout=0):
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    return db.insert_scenario(
        profile=profile,
        name="s1" if not holdout else "s_holdout",
        input="say hello",
        checks=[{"type": "must_contain", "value": "hello"}],
        holdout=holdout,
        created_at=ts,
    )


def test_run_eval_writes_eval_run_row(db):
    from _eval_runner import run_eval
    exp_id = _make_experiment(db)
    _make_scenario(db)

    scenario_runner = lambda inp: "hello world"
    score = run_eval(
        db=db,
        experiment_id=exp_id,
        profile="p",
        kind="offline",
        scenario_runner=scenario_runner,
        proposer_model="model-a",
        judge_model="model-b",
    )

    runs = db.list_eval_runs(exp_id)
    assert len(runs) == 1
    run = runs[0]
    assert run["kind"] == "offline"
    assert run["proposer_model"] == "model-a"
    assert run["judge_model"] == "model-b"
    assert run["aggregate_score"] == pytest.approx(score)


def test_run_eval_writes_scenario_results(db):
    from _eval_runner import run_eval
    exp_id = _make_experiment(db)
    _make_scenario(db)

    run_eval(
        db=db,
        experiment_id=exp_id,
        profile="p",
        kind="offline",
        scenario_runner=lambda inp: "hello world",
        proposer_model="model-a",
        judge_model="model-b",
    )

    runs = db.list_eval_runs(exp_id)
    results = db.list_scenario_results(runs[0]["id"])
    assert len(results) == 1
    r = results[0]
    assert r["split"] == "train"
    assert r["pass_fail"] == 1
    # scenario_snapshot must be a JSON string with input/checks
    import json
    snap = json.loads(r["scenario_snapshot"])
    assert "input" in snap
    assert "checks" in snap


def test_run_eval_holdout_excluded_by_default(db):
    from _eval_runner import run_eval
    exp_id = _make_experiment(db)
    _make_scenario(db, holdout=0)
    _make_scenario(db, holdout=1)

    run_eval(
        db=db,
        experiment_id=exp_id,
        profile="p",
        kind="offline",
        scenario_runner=lambda inp: "hello world",
        proposer_model="model-a",
        judge_model="model-b",
        include_holdout=False,
    )

    runs = db.list_eval_runs(exp_id)
    results = db.list_scenario_results(runs[0]["id"])
    splits = [r["split"] for r in results]
    assert "holdout" not in splits
    assert splits.count("train") == 1


def test_run_eval_holdout_included_when_requested(db):
    from _eval_runner import run_eval
    exp_id = _make_experiment(db)
    _make_scenario(db, holdout=0)
    _make_scenario(db, holdout=1)

    run_eval(
        db=db,
        experiment_id=exp_id,
        profile="p",
        kind="live",
        scenario_runner=lambda inp: "hello world",
        proposer_model="model-a",
        judge_model="model-b",
        include_holdout=True,
    )

    runs = db.list_eval_runs(exp_id)
    results = db.list_scenario_results(runs[0]["id"])
    splits = [r["split"] for r in results]
    assert "train" in splits
    assert "holdout" in splits


def test_run_eval_raises_if_proposer_equals_judge(db):
    from _eval_runner import run_eval
    exp_id = _make_experiment(db)
    _make_scenario(db)

    with pytest.raises(ValueError, match="must differ"):
        run_eval(
            db=db,
            experiment_id=exp_id,
            profile="p",
            kind="offline",
            scenario_runner=lambda inp: "hello",
            proposer_model="same-model",
            judge_model="same-model",
        )


def test_run_eval_no_scenarios_returns_zero(db):
    from _eval_runner import run_eval
    exp_id = _make_experiment(db)

    score = run_eval(
        db=db,
        experiment_id=exp_id,
        profile="p",
        kind="offline",
        scenario_runner=lambda inp: "hello",
        proposer_model="model-a",
        judge_model="model-b",
    )
    assert score == 0.0


def test_run_eval_scenario_snapshot_has_profile(db):
    from _eval_runner import run_eval
    import json
    exp_id = _make_experiment(db, profile="myprofile")
    _make_scenario(db, profile="myprofile")

    run_eval(
        db=db,
        experiment_id=exp_id,
        profile="myprofile",
        kind="offline",
        scenario_runner=lambda inp: "hello world",
        proposer_model="a",
        judge_model="b",
    )

    runs = db.list_eval_runs(exp_id)
    results = db.list_scenario_results(runs[0]["id"])
    snap = json.loads(results[0]["scenario_snapshot"])
    assert snap.get("profile") == "myprofile"
