"""
test_daemon.py — Tests for daemon.py tick functions.

Tests the tick logic directly without running the asyncio loop:
- _tick_live_experiments: live experiment with observed >= target gets
  verified when live eval improves, reverted when it drops
- paused profile is skipped by _tick_proposals
- _get_enabled_profiles returns profiles from scenarios + experiments
"""
from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure the karpathy plugin's daemon.py is always used, even if another
# daemon module (e.g. workflow-engine/daemon.py) was imported earlier in the
# test session.  We forcibly load it under the 'daemon' key in sys.modules
# from the known path before any test in this file runs.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_DAEMON_PATH = _PLUGIN_DIR / "daemon.py"


def _ensure_karpathy_daemon() -> None:
    current = sys.modules.get("daemon")
    if current is not None and getattr(current, "__file__", "") == str(_DAEMON_PATH):
        return  # already correct
    spec = importlib.util.spec_from_file_location("daemon", _DAEMON_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["daemon"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]


_ensure_karpathy_daemon()


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
def git_repo(tmp_path, patch_profiles_root):
    """Minimal git repo with a SOUL.md committed, placed under patched profiles root."""
    repo = patch_profiles_root / "profile_root"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    soul = repo / "SOUL.md"
    soul.write_text("You are a helpful assistant.\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "initial commit"], repo)
    return repo


def _now():
    return datetime.now(timezone.utc).isoformat()


def _make_live_experiment(db, profile, profile_root, *, observed=10, target=10):
    """Insert a live experiment with observed >= target (ready for verification)."""
    ts = _now()
    exp_id = db.insert_experiment(
        profile=profile,
        state="proposed",
        target_profile_root=str(profile_root),
        target_relpath="SOUL.md",
        proposer_model="model-a",
        judge_model="model-b",
        created_at=ts,
        updated_at=ts,
    )
    # Move to live state directly via update (bypassing state machine for test setup)
    db.update_experiment_fields(
        exp_id,
        state="live",
        live_sessions_target=target,
        live_sessions_observed=observed,
        updated_at=ts,
    )
    return exp_id


def _apply_commit_to_experiment(db, exp_id, git_repo):
    """Write a file + make a commit, store the sha on the experiment."""
    from _git_ratchet import apply_and_commit
    result = apply_and_commit(
        git_repo,
        "SOUL.md",
        b"You are a helpful assistant. Updated.\n",
        message="feat(karpathy): apply experiment",
    )
    assert result.ok, result.error
    db.update_experiment_fields(exp_id, apply_commit_sha=result.commit_sha, updated_at=_now())
    return result.commit_sha


# ---------------------------------------------------------------------------
# _tick_live_experiments — verified path
# ---------------------------------------------------------------------------

def test_tick_live_verifies_when_score_holds(db, git_repo):
    from daemon import _tick_live_experiments

    profile = "verify-profile"
    exp_id = _make_live_experiment(db, profile, git_repo, observed=10, target=10)
    _apply_commit_to_experiment(db, exp_id, git_repo)

    # Seed a baseline so the live score can be compared
    db.insert_baseline(
        profile=profile,
        file="SOUL.md",
        score=0.7,
        created_at=_now(),
    )

    # Mock run_eval to return a score >= baseline
    with patch("_eval_runner.run_eval", return_value=0.8) as mock_eval:
        _tick_live_experiments(db)

    exp = db.get_experiment(exp_id)
    assert exp["state"] == "verified"

    # A new baseline row should exist with the live score
    baselines = db.list_baselines(profile)
    # At least one baseline with score=0.8
    scores = [b["score"] for b in baselines]
    assert 0.8 in scores


def test_tick_live_reverts_when_score_drops(db, git_repo):
    from daemon import _tick_live_experiments

    profile = "revert-profile"
    exp_id = _make_live_experiment(db, profile, git_repo, observed=10, target=10)
    commit_sha = _apply_commit_to_experiment(db, exp_id, git_repo)

    # Seed a baseline higher than mock live score
    db.insert_baseline(
        profile=profile,
        file="SOUL.md",
        score=0.9,
        created_at=_now(),
    )

    with patch("_eval_runner.run_eval", return_value=0.5) as mock_eval:
        _tick_live_experiments(db)

    exp = db.get_experiment(exp_id)
    assert exp["state"] == "reverted"


def test_tick_live_revert_skips_file_io_when_profile_root_missing(db):
    """#176: a live experiment with no target_profile_root must not fall back
    to '.' (daemon CWD) for the revert. State still transitions to
    'reverted'; revert_commit must simply not be called."""
    from daemon import _tick_live_experiments

    profile = "no-root-profile"
    ts = _now()
    exp_id = db.insert_experiment(
        profile=profile,
        state="proposed",
        target_profile_root="",
        target_relpath="SOUL.md",
        proposer_model="model-a",
        judge_model="model-b",
        created_at=ts,
        updated_at=ts,
    )
    db.update_experiment_fields(
        exp_id,
        state="live",
        live_sessions_target=10,
        live_sessions_observed=10,
        apply_commit_sha="deadbeef",
        updated_at=ts,
    )
    db.insert_baseline(profile=profile, file="SOUL.md", score=0.9, created_at=ts)

    with patch("_eval_runner.run_eval", return_value=0.5), \
         patch("_git_ratchet.revert_commit") as mock_revert:
        _tick_live_experiments(db)

    mock_revert.assert_not_called()
    exp = db.get_experiment(exp_id)
    assert exp["state"] == "reverted"


def test_tick_live_skips_when_not_enough_sessions(db, git_repo):
    from daemon import _tick_live_experiments

    profile = "not-ready-profile"
    # observed=3, target=10 → should not trigger eval
    exp_id = _make_live_experiment(db, profile, git_repo, observed=3, target=10)

    with patch("_eval_runner.run_eval") as mock_eval:
        _tick_live_experiments(db)

    mock_eval.assert_not_called()
    exp = db.get_experiment(exp_id)
    assert exp["state"] == "live"  # unchanged


def test_tick_live_no_baseline_always_verifies(db, git_repo):
    from daemon import _tick_live_experiments

    profile = "no-baseline-profile"
    exp_id = _make_live_experiment(db, profile, git_repo, observed=10, target=10)
    _apply_commit_to_experiment(db, exp_id, git_repo)

    # No baseline inserted
    with patch("_eval_runner.run_eval", return_value=0.5):
        _tick_live_experiments(db)

    exp = db.get_experiment(exp_id)
    assert exp["state"] == "verified"


# ---------------------------------------------------------------------------
# _tick_proposals — paused profile skipped
# ---------------------------------------------------------------------------

def test_tick_proposals_skips_paused_profile(db):
    from daemon import _tick_proposals

    profile = "paused-profile"
    # Register the profile by adding a scenario
    db.insert_scenario(
        profile=profile,
        name="s",
        input="hi",
        created_at=_now(),
    )
    db.set_paused(profile, True)

    with patch("_proposer.propose_for_profile") as mock_propose:
        _tick_proposals(db)

    mock_propose.assert_not_called()


def test_tick_proposals_skips_when_active_experiment(db):
    from daemon import _tick_proposals

    profile = "active-exp-profile"
    ts = _now()
    db.insert_scenario(profile=profile, name="s", input="hi", created_at=ts)
    db.insert_experiment(
        profile=profile, state="proposed", created_at=ts, updated_at=ts
    )

    with patch("_proposer.propose_for_profile") as mock_propose:
        _tick_proposals(db)

    mock_propose.assert_not_called()


def test_tick_proposals_calls_propose_for_unpaused_profile(db):
    from daemon import _tick_proposals
    from _proposer import ProposalResult

    profile = "active-profile"
    db.insert_scenario(
        profile=profile,
        name="s",
        input="hi",
        created_at=_now(),
    )
    # Not paused, no active experiment

    mock_result = ProposalResult(ok=True, skipped=False, experiment_id=42, offline_score=0.8)
    # #176: target resolution now fails fast without a config block or prior
    # experiment — patch it directly rather than relying on the old implicit
    # "system_prompt.md" / "." default.
    with patch("_proposer.propose_for_profile", return_value=mock_result) as mock_propose, \
         patch("_wiring.resolve_target_for_profile", return_value=("system_prompt.md", ".")):
        _tick_proposals(db)

    mock_propose.assert_called_once()
    call_kwargs = mock_propose.call_args
    assert call_kwargs[1]["profile"] == profile or call_kwargs[0][1] == profile


# ---------------------------------------------------------------------------
# _get_enabled_profiles
# ---------------------------------------------------------------------------

def test_get_enabled_profiles_from_scenarios(db):
    from daemon import _get_enabled_profiles

    db.insert_scenario(profile="alpha", name="s", input="x", created_at=_now())
    db.insert_scenario(profile="beta", name="s", input="x", created_at=_now())

    profiles = _get_enabled_profiles(db)
    assert "alpha" in profiles
    assert "beta" in profiles


def test_get_enabled_profiles_from_experiments(db):
    from daemon import _get_enabled_profiles

    ts = _now()
    db.insert_experiment(profile="gamma", state="proposed", created_at=ts, updated_at=ts)

    profiles = _get_enabled_profiles(db)
    assert "gamma" in profiles


def test_get_enabled_profiles_empty(db):
    from daemon import _get_enabled_profiles
    assert _get_enabled_profiles(db) == []


# ---------------------------------------------------------------------------
# _cmd_bootstrap / _cmd_pause / _cmd_resume (#176)
# ---------------------------------------------------------------------------

def test_cmd_bootstrap_writes_config_and_starts_paused(tmp_path, db, monkeypatch):
    from daemon import _cmd_bootstrap
    import _db as db_mod

    profile_root = tmp_path / "prof-root"
    profile_root.mkdir()
    (profile_root / "SOUL.md").write_text("You are helpful.\n")

    monkeypatch.setattr(db_mod, "get_db", lambda: db)

    saved = {}
    with patch("hermes_cli.profiles.get_profile_dir", return_value=profile_root), \
         patch("hermes_cli.config.load_config", return_value={}), \
         patch("hermes_cli.config.save_config", side_effect=saved.update):
        _cmd_bootstrap("prof")

    block = saved["plugins"]["karpathy_self_improve"]["profiles"]["prof"]
    assert block["target_relpath"] == "SOUL.md"
    assert block["profile_root"] == str(profile_root)
    assert block["paused"] is True
    assert db.is_paused("prof") is True


def test_cmd_bootstrap_prefers_soul_over_system_prompt(tmp_path, db, monkeypatch):
    """Identity-file priority: SOUL.md wins over system_prompt.md when both exist."""
    from daemon import _cmd_bootstrap
    import _db as db_mod

    profile_root = tmp_path / "prof-root"
    profile_root.mkdir()
    (profile_root / "system_prompt.md").write_text("legacy\n")
    (profile_root / "SOUL.md").write_text("current\n")

    monkeypatch.setattr(db_mod, "get_db", lambda: db)

    saved = {}
    with patch("hermes_cli.profiles.get_profile_dir", return_value=profile_root), \
         patch("hermes_cli.config.load_config", return_value={}), \
         patch("hermes_cli.config.save_config", side_effect=saved.update):
        _cmd_bootstrap("prof")

    block = saved["plugins"]["karpathy_self_improve"]["profiles"]["prof"]
    assert block["target_relpath"] == "SOUL.md"


def test_cmd_bootstrap_errors_when_no_identity_file(tmp_path, capsys):
    from daemon import _cmd_bootstrap

    profile_root = tmp_path / "prof-root"
    profile_root.mkdir()

    with patch("hermes_cli.profiles.get_profile_dir", return_value=profile_root):
        _cmd_bootstrap("prof")

    assert "no identity file" in capsys.readouterr().err


def test_cmd_bootstrap_errors_when_profile_dir_missing(tmp_path, capsys):
    from daemon import _cmd_bootstrap

    missing = tmp_path / "does-not-exist"
    with patch("hermes_cli.profiles.get_profile_dir", return_value=missing):
        _cmd_bootstrap("prof")

    assert "does not exist" in capsys.readouterr().err


def test_cmd_pause_and_resume(db, monkeypatch):
    from daemon import _cmd_pause, _cmd_resume
    import _db as db_mod

    monkeypatch.setattr(db_mod, "get_db", lambda: db)

    _cmd_pause("prof")
    assert db.is_paused("prof") is True

    _cmd_resume("prof")
    assert db.is_paused("prof") is False


def test_resolve_cli_profile_reads_global_active_profile(monkeypatch):
    """#180: per-profile subcommands take the profile from the global
    `hermes --profile <name>` (HERMES_HOME), not a subcommand --profile flag."""
    import daemon
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "hermes-switch")
    assert daemon._resolve_cli_profile("bootstrap") == "hermes-switch"


def test_resolve_cli_profile_errors_without_named_profile(monkeypatch, capsys):
    """No `--profile` selected (default/empty) must fail with an actionable
    message telling the operator to use `hermes --profile <name> karpathy ...`."""
    import daemon
    import hermes_cli.profiles as profiles

    for value in ("default", ""):
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: value)
        with pytest.raises(SystemExit) as exc:
            daemon._resolve_cli_profile("bootstrap")
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "hermes --profile <name> karpathy bootstrap" in err
