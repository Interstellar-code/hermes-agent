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
    # The edit rewrites exactly 1 sentence in-place → diff-based count == 1
    assert result.sentence_delta_count == 1


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


# ---------------------------------------------------------------------------
# Pause guard tests (FIX 1)
# ---------------------------------------------------------------------------

def test_propose_skips_when_profile_paused(db, git_repo):
    """Paused profile → propose returns skipped with skip_reason 'profile is paused'."""
    from _proposer import propose_for_profile

    profile = "paused-profile"
    _make_scenario(db, profile)
    db.set_paused(profile, True)

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
    assert result.skip_reason == "profile is paused"


def test_propose_proceeds_after_resume(db, git_repo):
    """After resume, propose proceeds normally (does not skip for pause)."""
    from _proposer import propose_for_profile

    profile = "resumed-profile"
    _make_scenario(db, profile)
    db.set_paused(profile, True)
    db.set_paused(profile, False)

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

    # Should not be skipped due to pause (may still skip for score, but not pause)
    assert not (result.skipped and result.skip_reason == "profile is paused")


# ---------------------------------------------------------------------------
# Atomicity metric tests (FIX 2)
# ---------------------------------------------------------------------------

# In-place single-sentence edit: "helpful" → "super helpful" in one sentence.
_INPLACE_ONE_SENTENCE_DIFF = """\
--- a/SOUL.md
+++ b/SOUL.md
@@ -1 +1 @@
-You are a helpful assistant. Always be concise.
+You are a super helpful assistant. Always be concise.
"""
_INPLACE_ONE_SENTENCE_RESPONSE = (
    f"DIFF:\n{_INPLACE_ONE_SENTENCE_DIFF}\nRATIONALE:\nMakes the assistant more helpful.\n"
)


def test_atomicity_inplace_single_sentence_edit_accepted(db, git_repo):
    """In-place single-sentence rewrite → accepted, changed-count == 1."""
    from _proposer import propose_for_profile

    profile = "atomicity-single-profile"
    _make_scenario(db, profile)

    result = propose_for_profile(
        db=db,
        profile=profile,
        target_relpath="SOUL.md",
        profile_root=str(git_repo),
        llm_fn=lambda prompt: _INPLACE_ONE_SENTENCE_RESPONSE,
        scenario_runner=lambda inp: "hello",
        proposer_model="a",
        judge_model="b",
    )

    # Must not be rejected for atomicity (ok=True, not skipped for atomicity)
    assert result.ok is True
    assert result.sentence_delta_count == 1


# In-place rewrite of two sentences (same total count) — the regression case.
# Original: 2 sentences. Modified: 2 sentences, both changed.
_INPLACE_TWO_SENTENCE_DIFF = """\
--- a/SOUL.md
+++ b/SOUL.md
@@ -1 +1 @@
-You are a helpful assistant. Always be concise.
+You are an extremely capable assistant. Always be brief and direct.
"""
_INPLACE_TWO_SENTENCE_RESPONSE = (
    f"DIFF:\n{_INPLACE_TWO_SENTENCE_DIFF}\nRATIONALE:\nRewrites both sentences.\n"
)


def test_atomicity_inplace_two_sentence_rewrite_rejected(db, git_repo):
    """In-place rewrite of 2 sentences (same count) → REJECTED. Regression test for FIX 2."""
    from _proposer import propose_for_profile

    profile = "atomicity-two-inplace-profile"
    _make_scenario(db, profile)

    result = propose_for_profile(
        db=db,
        profile=profile,
        target_relpath="SOUL.md",
        profile_root=str(git_repo),
        llm_fn=lambda prompt: _INPLACE_TWO_SENTENCE_RESPONSE,
        scenario_runner=lambda inp: "hello",
        proposer_model="a",
        judge_model="b",
    )

    # Must be rejected: changed-sentence count > 1
    assert result.ok is False
    assert result.sentence_delta_count > 1
    assert "sentences" in result.error.lower() or "atomic" in result.error.lower()
    # No proposed experiment should remain
    proposed = db.list_experiments(profile=profile, state="proposed")
    assert len(proposed) == 0


def test_atomicity_net_add_two_sentences_rejected(db, git_repo):
    """Net add of 2 new sentences → rejected."""
    from _proposer import propose_for_profile

    net_add_two_diff = """\
--- a/SOUL.md
+++ b/SOUL.md
@@ -1 +1,3 @@
-You are a helpful assistant. Always be concise.
+You are a helpful assistant. Always be concise.
+Be clear.
+Stay on topic.
"""
    response = f"DIFF:\n{net_add_two_diff}\nRATIONALE:\nAdds two directives.\n"

    profile = "atomicity-net-add-two-profile"
    _make_scenario(db, profile)

    result = propose_for_profile(
        db=db,
        profile=profile,
        target_relpath="SOUL.md",
        profile_root=str(git_repo),
        llm_fn=lambda prompt: response,
        scenario_runner=lambda inp: "hello",
        proposer_model="a",
        judge_model="b",
    )

    assert result.ok is False
    assert result.sentence_delta_count > 1


def test_atomicity_net_add_one_sentence_accepted(db, git_repo):
    """Net add of exactly 1 new sentence → accepted, count == 1."""
    from _proposer import propose_for_profile

    net_add_one_diff = """\
--- a/SOUL.md
+++ b/SOUL.md
@@ -1 +1,2 @@
-You are a helpful assistant. Always be concise.
+You are a helpful assistant. Always be concise.
+Be clear.
"""
    response = f"DIFF:\n{net_add_one_diff}\nRATIONALE:\nAdds one directive.\n"

    profile = "atomicity-net-add-one-profile"
    _make_scenario(db, profile)

    result = propose_for_profile(
        db=db,
        profile=profile,
        target_relpath="SOUL.md",
        profile_root=str(git_repo),
        llm_fn=lambda prompt: response,
        scenario_runner=lambda inp: "hello",
        proposer_model="a",
        judge_model="b",
    )

    # Should not be rejected for atomicity
    assert result.ok is True
    assert result.sentence_delta_count == 1
