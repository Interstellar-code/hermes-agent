"""
test_security_fixes.py — Regression tests for Gate 2 security fixes.

Covers:
  C-1  Path traversal: relpath with '..' rejected by apply_and_commit + API (400)
  C-1  Profile root outside ~/.hermes/profiles rejected by apply_and_commit + revert_commit
  C-2  Invalid SHA rejected before git revert
  H-1  update_experiment_fields _commit=False keeps transition atomic
  H-2  #173: DB transaction failure during apply rolls back atomically —
       state stays 'approved' and the target file is never written
  H-5  run_eval raises when judge_model is None / same as proposer_model
  H-6  Revert handler returns 500 and keeps state when git revert fails
  H-4  POST /experiments rejects '..' in file field (400)
  M-4  Daemon auto-reverts experiment after N consecutive eval failures
"""
from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(args: list, cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, check=True)


def _make_git_repo(path: Path) -> Path:
    """Create a minimal git repo with one committed file."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    f = path / "prompt.md"
    f.write_text("# original\n")
    _git(["add", "."], path)
    _git(["commit", "-m", "initial"], path)
    return path


# ---------------------------------------------------------------------------
# C-1: Path traversal — apply_and_commit
# ---------------------------------------------------------------------------

class TestC1PathTraversal:
    def test_apply_and_commit_rejects_dotdot_relpath(self, tmp_path, monkeypatch):
        """apply_and_commit must reject a relpath that escapes the root."""
        import _git_ratchet
        repo = _make_git_repo(tmp_path / "profiles" / "p1")
        monkeypatch.setattr(_git_ratchet, "_PROFILES_ROOT", tmp_path / "profiles")

        result = _git_ratchet.apply_and_commit(
            repo,
            "../../etc/passwd",
            b"evil",
            "should be rejected",
        )
        assert not result.ok
        assert "traversal" in result.error.lower() or "escape" in result.error.lower()

        # No file written outside root.
        assert not (tmp_path / "etc" / "passwd").exists()

    def test_apply_and_commit_rejects_profile_root_outside_profiles(self, tmp_path, monkeypatch):
        """apply_and_commit must reject a root that is not under _PROFILES_ROOT."""
        import _git_ratchet
        # Set profiles root to a sub-dir; use a repo outside of it.
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        repo = _make_git_repo(tmp_path / "elsewhere" / "repo")
        monkeypatch.setattr(_git_ratchet, "_PROFILES_ROOT", profiles)

        result = _git_ratchet.apply_and_commit(
            repo, "prompt.md", b"evil", "should be rejected"
        )
        assert not result.ok
        assert "not inside" in result.error

    def test_revert_commit_rejects_profile_root_outside_profiles(self, tmp_path, monkeypatch):
        """revert_commit must reject a root not under _PROFILES_ROOT."""
        import _git_ratchet
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        repo = _make_git_repo(tmp_path / "elsewhere" / "repo")
        monkeypatch.setattr(_git_ratchet, "_PROFILES_ROOT", profiles)

        result = _git_ratchet.revert_commit(
            repo,
            "a" * 40,  # valid SHA format
            "should be rejected",
        )
        assert not result.ok
        assert "not inside" in result.error


# ---------------------------------------------------------------------------
# C-2: SHA validation
# ---------------------------------------------------------------------------

class TestC2ShaValidation:
    def test_revert_commit_rejects_short_sha(self, tmp_path, monkeypatch):
        import _git_ratchet
        profiles = tmp_path / "profiles"
        repo = _make_git_repo(profiles / "p1")
        monkeypatch.setattr(_git_ratchet, "_PROFILES_ROOT", profiles)

        result = _git_ratchet.revert_commit(repo, "deadbeef", "msg")
        assert not result.ok
        assert "invalid" in result.error.lower() or "sha" in result.error.lower()

    def test_revert_commit_rejects_flag_like_sha(self, tmp_path, monkeypatch):
        """A git-flag-like string like '--no-commit' must be rejected."""
        import _git_ratchet
        profiles = tmp_path / "profiles"
        repo = _make_git_repo(profiles / "p1")
        monkeypatch.setattr(_git_ratchet, "_PROFILES_ROOT", profiles)

        result = _git_ratchet.revert_commit(repo, "--no-commit", "msg")
        assert not result.ok

    def test_revert_commit_rejects_head_ref(self, tmp_path, monkeypatch):
        """'HEAD~1' is not a 40-hex SHA and must be rejected."""
        import _git_ratchet
        profiles = tmp_path / "profiles"
        repo = _make_git_repo(profiles / "p1")
        monkeypatch.setattr(_git_ratchet, "_PROFILES_ROOT", profiles)

        result = _git_ratchet.revert_commit(repo, "HEAD~1", "msg")
        assert not result.ok

    def test_revert_commit_accepts_valid_sha_format(self, tmp_path, monkeypatch):
        """A well-formed 40-hex SHA passes validation (git may still fail — that's OK)."""
        import _git_ratchet
        profiles = tmp_path / "profiles"
        repo = _make_git_repo(profiles / "p1")
        monkeypatch.setattr(_git_ratchet, "_PROFILES_ROOT", profiles)

        # Valid format but non-existent commit — git fails but for the right reason.
        result = _git_ratchet.revert_commit(repo, "a" * 40, "msg")
        # Should not get a SHA-validation error.
        assert "invalid" not in result.error.lower() or "sha" not in result.error.lower()


# ---------------------------------------------------------------------------
# H-4: POST /experiments input validation
# ---------------------------------------------------------------------------

def _make_test_client(tmp_path, monkeypatch, patch_profiles_root):
    """Return a FastAPI TestClient with an isolated DB and patched profiles root."""
    db_file = str(tmp_path / "sec-test.db")
    monkeypatch.setenv("KARPATHY_DB_PATH", db_file)

    import _db as db_mod
    monkeypatch.setattr(db_mod, "_conn", None)
    for k in [k for k in sys.modules if k.startswith("_db")]:
        del sys.modules[k]

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    if "dashboard.plugin_api" in sys.modules:
        del sys.modules["dashboard.plugin_api"]
    spec = importlib.util.spec_from_file_location(
        "dashboard.plugin_api",
        _PLUGIN_DIR / "dashboard" / "plugin_api.py",
    )
    api_mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(api_mod)  # type: ignore[union-attr]

    app = FastAPI()
    app.include_router(api_mod.router)
    return TestClient(app)


class TestH4InputValidation:
    def test_create_experiment_rejects_dotdot_file(self, tmp_path, monkeypatch, patch_profiles_root):
        """POST /experiments must reject file containing '..'."""
        # Create a valid profile dir.
        (patch_profiles_root / "myprofile").mkdir(parents=True, exist_ok=True)
        tc = _make_test_client(tmp_path, monkeypatch, patch_profiles_root)

        resp = tc.post("/experiments", json={
            "profile": "myprofile",
            "file": "../../etc/cron.d/evil",
            "diff": "",
            "rationale": "attack",
        })
        assert resp.status_code == 400
        assert ".." in resp.json()["error"] or "relative" in resp.json()["error"]

    def test_create_experiment_rejects_absolute_file(self, tmp_path, monkeypatch, patch_profiles_root):
        """POST /experiments must reject file starting with '/'."""
        (patch_profiles_root / "myprofile").mkdir(parents=True, exist_ok=True)
        tc = _make_test_client(tmp_path, monkeypatch, patch_profiles_root)

        resp = tc.post("/experiments", json={
            "profile": "myprofile",
            "file": "/etc/passwd",
            "diff": "",
            "rationale": "attack",
        })
        assert resp.status_code == 400

    def test_create_experiment_rejects_nonexistent_profile(self, tmp_path, monkeypatch, patch_profiles_root):
        """POST /experiments must reject a profile dir that doesn't exist."""
        tc = _make_test_client(tmp_path, monkeypatch, patch_profiles_root)

        resp = tc.post("/experiments", json={
            "profile": "no-such-profile",
            "file": "SOUL.md",
            "diff": "",
            "rationale": "test",
        })
        assert resp.status_code == 400

    def test_create_experiment_accepts_valid_input(self, tmp_path, monkeypatch, patch_profiles_root):
        """POST /experiments succeeds with a valid profile and safe file path."""
        (patch_profiles_root / "good-profile").mkdir(parents=True, exist_ok=True)
        tc = _make_test_client(tmp_path, monkeypatch, patch_profiles_root)

        resp = tc.post("/experiments", json={
            "profile": "good-profile",
            "file": "subdir/SOUL.md",
            "diff": "",
            "rationale": "ok",
        })
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# H-5: Judge guard
# ---------------------------------------------------------------------------

class TestH5JudgeGuard:
    def test_run_eval_raises_when_judge_model_is_none(self, tmp_path, monkeypatch):
        """run_eval must raise ValueError when judge_model is None."""
        import _db
        db = _db.open_db(tmp_path / "h5.db")

        from _eval_runner import run_eval
        with pytest.raises(ValueError, match="explicitly set"):
            run_eval(
                db=db,
                experiment_id=1,
                profile="test",
                kind="offline",
                proposer_model="claude-sonnet-4-6",
                judge_model=None,
            )

    def test_run_eval_raises_when_proposer_model_is_none(self, tmp_path, monkeypatch):
        """run_eval must raise ValueError when proposer_model is None."""
        import _db
        db = _db.open_db(tmp_path / "h5b.db")

        from _eval_runner import run_eval
        with pytest.raises(ValueError, match="explicitly set"):
            run_eval(
                db=db,
                experiment_id=1,
                profile="test",
                kind="offline",
                proposer_model=None,
                judge_model="claude-sonnet-4-6",
            )

    def test_run_eval_allows_equal_models_after_guard_disabled(self, tmp_path):
        """Anti-gaming guard intentionally disabled: equal proposer/judge models no
        longer raise (operator opt-in, e.g. both 'auto'). run_eval warns and
        proceeds; with no scenarios it returns a score without raising."""
        import _db
        db = _db.open_db(tmp_path / "h5c.db")

        from _eval_runner import run_eval
        score = run_eval(
            db=db,
            experiment_id=1,
            profile="test",
            kind="offline",
            proposer_model="model-a",
            judge_model="model-a",
        )
        assert isinstance(score, (int, float))


# ---------------------------------------------------------------------------
# H-6: Revert handler returns 500 and keeps state when git revert fails
# ---------------------------------------------------------------------------

class TestH6RevertFailure:
    def test_revert_handler_returns_500_when_git_revert_fails(
        self, tmp_path, monkeypatch, patch_profiles_root
    ):
        """If revert_commit returns ok=False, the endpoint must return 500
        and must NOT transition the experiment to 'reverted'."""
        import _git_ratchet
        # Create a valid profile dir under patched root.
        profile_dir = patch_profiles_root / "rev-profile"
        profile_dir.mkdir(parents=True, exist_ok=True)

        tc = _make_test_client(tmp_path, monkeypatch, patch_profiles_root)

        # Create and approve an experiment.
        resp = tc.post("/experiments", json={
            "profile": "rev-profile",
            "file": "SOUL.md",
            "diff": "",
            "rationale": "h6 test",
        })
        assert resp.status_code == 201
        exp_id = resp.json()["experiment_id"]

        # Manually set it to 'live' with a fake apply_commit_sha and profile root.
        from _db import open_db
        db = open_db(tmp_path / "sec-test.db")
        db.update_experiment_fields(
            exp_id,
            state="live",
            apply_commit_sha="a" * 40,
            target_profile_root=str(profile_dir),
            target_relpath="SOUL.md",
            updated_at="2026-01-01T00:00:00+00:00",
        )

        # Mock revert_commit to simulate failure.
        from _git_ratchet import RevertResult
        with patch("_git_ratchet.revert_commit", return_value=RevertResult(ok=False, error="conflict")) as mock_revert:
            resp = tc.post(f"/experiments/{exp_id}/revert", json={"reason": "test"})

        assert resp.status_code == 500
        assert "git revert failed" in resp.json()["error"]

        # State must NOT have changed to 'reverted'.
        db2 = open_db(tmp_path / "sec-test.db")
        exp = db2.get_experiment(exp_id)
        assert exp["state"] == "live"


# ---------------------------------------------------------------------------
# H-2: #173 DB-transaction atomicity on apply
# ---------------------------------------------------------------------------

class TestH2CompensatingRevert:
    def test_apply_rolls_back_and_leaves_file_untouched_when_db_write_fails(
        self, tmp_path, monkeypatch, patch_profiles_root
    ):
        """If the snapshot/state-transition DB transaction raises partway
        through, the whole transaction must roll back (state stays
        'approved', no snapshot persisted) and the target file must never be
        written — there is no git commit to compensate-revert anymore since
        the DB transaction now commits before the file write."""
        profile_dir = _make_git_repo(patch_profiles_root / "comp-profile")
        target_path = profile_dir / "prompt.md"
        original_bytes = target_path.read_bytes()

        tc = _make_test_client(tmp_path, monkeypatch, patch_profiles_root)

        resp = tc.post("/experiments", json={
            "profile": "comp-profile",
            "file": "prompt.md",
            "diff": "",
            "rationale": "h2 test",
        })
        assert resp.status_code == 201
        exp_id = resp.json()["experiment_id"]

        from _db import open_db
        db = open_db(tmp_path / "sec-test.db")
        db.update_experiment_fields(
            exp_id,
            state="approved",
            target_profile_root=str(profile_dir),
            target_relpath="prompt.md",
            updated_at="2026-01-01T00:00:00+00:00",
        )

        with patch("_db.KarpathyDB.insert_state_transition", side_effect=RuntimeError("DB crashed")):
            resp = tc.post(f"/experiments/{exp_id}/apply")

        assert resp.status_code == 500

        # File must be untouched — the DB transaction failed before the write.
        assert target_path.read_bytes() == original_bytes

        # State must still be 'approved', not 'live', and no snapshot row.
        db2 = open_db(tmp_path / "sec-test.db")
        exp = db2.get_experiment(exp_id)
        assert exp["state"] == "approved"
        assert db2.get_snapshot(exp_id) is None


# ---------------------------------------------------------------------------
# M-4: Daemon auto-reverts after N consecutive failures
# ---------------------------------------------------------------------------

class TestM4ConsecutiveFailures:
    def _load_daemon(self):
        """Load karpathy daemon module (force correct path)."""
        daemon_path = _PLUGIN_DIR / "daemon.py"
        spec = importlib.util.spec_from_file_location("daemon", daemon_path)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    def test_daemon_auto_reverts_after_max_consecutive_failures(self, tmp_path, monkeypatch):
        """After _MAX_CONSECUTIVE_TICK_FAILURES consecutive eval failures,
        the experiment is transitioned to 'reverted'."""
        import _db as db_mod
        monkeypatch.setenv("KARPATHY_DB_PATH", str(tmp_path / "m4.db"))
        monkeypatch.setattr(db_mod, "_conn", None)
        db = db_mod.open_db(tmp_path / "m4.db")

        now = "2026-01-01T00:00:00+00:00"
        exp_id = db.insert_experiment(
            profile="m4-profile",
            file="SOUL.md",
            state="proposed",
            diff="",
            rationale="m4 test",
            created_at=now,
            updated_at=now,
        )
        db.update_experiment_fields(
            exp_id,
            state="live",
            apply_commit_sha="a" * 40,
            target_profile_root=str(tmp_path),
            live_sessions_observed=10,
            live_sessions_target=5,
            proposer_model="p-model",
            judge_model="j-model",
            updated_at=now,
        )

        daemon = self._load_daemon()
        # Clear the failure counter for this exp_id.
        daemon._consecutive_tick_failures.clear()

        transition_calls = []

        def fake_run_eval(**kwargs):
            raise RuntimeError("eval exploded")

        def fake_transition(db, exp_id, to_state, **kwargs):
            transition_calls.append((exp_id, to_state))

        N = daemon._MAX_CONSECUTIVE_TICK_FAILURES
        # run_eval and transition are imported locally inside _tick_live_experiments,
        # so we patch them at the source module level.
        with patch("_eval_runner.run_eval", side_effect=fake_run_eval), \
             patch("_state_machine.transition", side_effect=fake_transition):
            for _ in range(N):
                daemon._tick_live_experiments(db)

        # After N failures, transition to 'reverted' must have been called.
        revert_calls = [(eid, st) for eid, st in transition_calls if st == "reverted"]
        assert len(revert_calls) >= 1
        assert revert_calls[0][0] == exp_id
