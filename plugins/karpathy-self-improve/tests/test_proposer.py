"""
test_proposer.py — Tests for _proposer.py.

Covers:
- propose_for_profile with mock llm_fn returning a single-sentence edit
  creates experiment in state='proposed' with correct provenance fields
- multi-sentence edit (delta > 1) rejected — no experiment kept
- skips when an active experiment already exists for the profile
- returns ProposalResult with ok=True, experiment_id set on success
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test-karpathy.db")
    monkeypatch.setenv("KARPATHY_DB_PATH", db_file)

    import _db as db_mod
    monkeypatch.setattr(db_mod, "_conn", None)

    from _db import open_db
    return open_db(Path(db_file))


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, check=True)


@pytest.fixture()
def git_repo(tmp_path):
    """Minimal git repo with a SOUL.md committed."""
    repo = tmp_path / "profile_root"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    soul = repo / "SOUL.md"
    soul.write_text("You are a helpful assistant. Always be concise.\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "initial commit"], repo)
    return repo


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _make_scenario(db, profile, *, holdout=0):
    db.insert_scenario(
        profile=profile,
        name="basic",
        input="say hello",
        checks=[{"type": "must_contain", "value": "hello"}],
        holdout=holdout,
        created_at=_now(),
    )


# A minimal valid unified diff that changes exactly one sentence in SOUL.md.
_GOOD_DIFF = """\
--- a/SOUL.md
+++ b/SOUL.md
@@ -1 +1 @@
-You are a helpful assistant. Always be concise.
+You are a helpful assistant. Always be concise and precise.
"""

_GOOD_LLM_RESPONSE = f"DIFF:\n{_GOOD_DIFF}\nRATIONALE:\nAdds precision to the conciseness directive.\n"


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

def test_propose_creates_experiment(db, git_repo):
    from _proposer import propose_for_profile

    profile = "test-profile"
    _make_scenario(db, profile)

    # scenario_runner always returns "hello" so the must_contain check passes
    result = propose_for_profile(
        db=db,
        profile=profile,
        target_relpath="SOUL.md",
        profile_root=str(git_repo),
        llm_fn=lambda prompt: _GOOD_LLM_RESPONSE,
        scenario_runner=lambda inp: "hello",
        proposer_model="proposer-model",
        judge_model="judge-model",
    )

    assert result.ok is True
    assert result.skipped is False
    assert result.experiment_id is not None

    exp = db.get_experiment(result.experiment_id)
    assert exp is not None
    assert exp["state"] == "proposed"
    assert exp["proposer_model"] == "proposer-model"
    assert exp["judge_model"] == "judge-model"
    assert exp["base_commit_sha"] is not None and len(exp["base_commit_sha"]) > 0
    assert exp["sentence_delta_count"] is not None


def test_propose_sets_sentence_delta_count(db, git_repo):
    from _proposer import propose_for_profile

    profile = "delta-profile"
    _make_scenario(db, profile)

    result = propose_for_profile(
        db=db,
        profile=profile,
        target_relpath="SOUL.md",
        profile_root=str(git_repo),
        llm_fn=lambda prompt: _GOOD_LLM_RESPONSE,
        scenario_runner=lambda inp: "hello",
        proposer_model="a",
        judge_model="b",
    )

    assert result.ok is True
    assert result.sentence_delta_count is not None
    # The edit changes <=1 sentence
    assert result.sentence_delta_count <= 1


# ---------------------------------------------------------------------------
# Multi-sentence edit rejected
# ---------------------------------------------------------------------------

_MULTI_SENTENCE_DIFF = """\
--- a/SOUL.md
+++ b/SOUL.md
@@ -1 +1,3 @@
-You are a helpful assistant. Always be concise.
+You are a helpful assistant. Always be concise. Never be verbose. Be direct.
+Be clear. Stay on topic.
"""

_MULTI_SENTENCE_RESPONSE = (
    f"DIFF:\n{_MULTI_SENTENCE_DIFF}\nRATIONALE:\nAdded multiple sentences.\n"
)


def test_propose_rejects_multi_sentence_edit(db, git_repo):
    from _proposer import propose_for_profile

    profile = "multi-sent-profile"
    _make_scenario(db, profile)

    result = propose_for_profile(
        db=db,
        profile=profile,
        target_relpath="SOUL.md",
        profile_root=str(git_repo),
        llm_fn=lambda prompt: _MULTI_SENTENCE_RESPONSE,
        scenario_runner=lambda inp: "hello",
        proposer_model="a",
        judge_model="b",
    )

    # Result is not ok (or is ok but skipped) — no experiment in 'proposed' state
    proposed = db.list_experiments(profile=profile, state="proposed")
    assert len(proposed) == 0


# ---------------------------------------------------------------------------
# Skips when active experiment already exists
# ---------------------------------------------------------------------------

def test_propose_skips_when_active_experiment_exists(db, git_repo):
    from _proposer import propose_for_profile

    profile = "skip-profile"
    ts = _now()
    # Insert a live experiment manually
    db.insert_experiment(
        profile=profile,
        state="proposed",
        created_at=ts,
        updated_at=ts,
    )

    result = propose_for_profile(
        db=db,
        profile=profile,
        target_relpath="SOUL.md",
        profile_root=str(git_repo),
        llm_fn=lambda prompt: _GOOD_LLM_RESPONSE,
        scenario_runner=lambda inp: "hello",
        proposer_model="a",
        judge_model="b",
    )

    assert result.ok is True
    assert result.skipped is True
    assert "active" in result.skip_reason.lower() or profile in result.skip_reason


def test_propose_skips_when_live_experiment_exists(db, git_repo):
    from _proposer import propose_for_profile

    profile = "live-profile"
    ts = _now()
    exp_id = db.insert_experiment(
        profile=profile,
        state="proposed",
        created_at=ts,
        updated_at=ts,
    )
    # Move it to live so it's still active
    db.update_experiment_fields(exp_id, state="live", updated_at=ts)

    result = propose_for_profile(
        db=db,
        profile=profile,
        target_relpath="SOUL.md",
        profile_root=str(git_repo),
        llm_fn=lambda prompt: _GOOD_LLM_RESPONSE,
        scenario_runner=lambda inp: "hello",
        proposer_model="a",
        judge_model="b",
    )

    assert result.skipped is True


# ---------------------------------------------------------------------------
# LLM error paths
# ---------------------------------------------------------------------------

def test_propose_handles_empty_llm_response(db, git_repo):
    from _proposer import propose_for_profile

    profile = "empty-llm-profile"
    _make_scenario(db, profile)

    result = propose_for_profile(
        db=db,
        profile=profile,
        target_relpath="SOUL.md",
        profile_root=str(git_repo),
        llm_fn=lambda prompt: "",
        scenario_runner=lambda inp: "hello",
        proposer_model="a",
        judge_model="b",
    )

    assert result.ok is False
    assert "empty" in result.error.lower() or "LLM" in result.error


def test_propose_handles_missing_target_file(db, git_repo):
    from _proposer import propose_for_profile

    profile = "missing-file-profile"
    result = propose_for_profile(
        db=db,
        profile=profile,
        target_relpath="nonexistent.md",
        profile_root=str(git_repo),
        llm_fn=lambda prompt: _GOOD_LLM_RESPONSE,
        scenario_runner=lambda inp: "hello",
        proposer_model="a",
        judge_model="b",
    )

    assert result.ok is False
    assert "not found" in result.error.lower() or "nonexistent" in result.error
