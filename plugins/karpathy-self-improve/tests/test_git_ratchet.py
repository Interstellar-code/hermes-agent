"""
test_git_ratchet.py — Git ratchet module tests.

Uses a real `git init` in tmp_path so git operations actually execute.
Tests cover:
  - current_commit_sha, blob_sha
  - apply_and_commit: changes exactly one file, returns base/new shas
  - revert_commit: restores file to pre-apply state
  - detect_manual_conflict: true when file changed out-of-band
  - file_hash: stable SHA-256 hash
  - guard: non-git dir returns ok=False with error
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture()
def git_repo(tmp_path, patch_profiles_root) -> Path:
    """Create a minimal git repo with one committed file.

    Uses patch_profiles_root so the repo lives under the patched profiles root,
    satisfying _assert_profile_root in test runs.
    """
    repo = patch_profiles_root / "profile"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)

    # Initial commit with the target file.
    target = repo / "agent" / "prompt.py"
    target.parent.mkdir(parents=True)
    target.write_text("# original content\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "initial commit"], repo)
    return repo


# ---------------------------------------------------------------------------
# current_commit_sha
# ---------------------------------------------------------------------------

def test_current_commit_sha(git_repo) -> None:
    from _git_ratchet import current_commit_sha
    result = current_commit_sha(git_repo)
    assert result.ok
    assert len(result.sha) == 40


def test_current_commit_sha_non_git_dir(tmp_path) -> None:
    from _git_ratchet import current_commit_sha
    bad = tmp_path / "notgit"
    bad.mkdir()
    result = current_commit_sha(bad)
    assert not result.ok
    assert result.error


def test_current_commit_sha_missing_dir(tmp_path) -> None:
    from _git_ratchet import current_commit_sha
    result = current_commit_sha(tmp_path / "does_not_exist")
    assert not result.ok


# ---------------------------------------------------------------------------
# blob_sha
# ---------------------------------------------------------------------------

def test_blob_sha(git_repo) -> None:
    from _git_ratchet import blob_sha
    result = blob_sha(git_repo, "agent/prompt.py")
    assert result.ok
    assert len(result.sha) == 40


def test_blob_sha_missing_file(git_repo) -> None:
    from _git_ratchet import blob_sha
    result = blob_sha(git_repo, "no/such/file.py")
    assert not result.ok


# ---------------------------------------------------------------------------
# file_hash
# ---------------------------------------------------------------------------

def test_file_hash_stable(git_repo) -> None:
    from _git_ratchet import file_hash
    r1 = file_hash(git_repo, "agent/prompt.py")
    r2 = file_hash(git_repo, "agent/prompt.py")
    assert r1.ok
    assert r1.sha == r2.sha
    assert len(r1.sha) == 64  # SHA-256 hex


def test_file_hash_changes_with_content(git_repo) -> None:
    from _git_ratchet import file_hash
    r1 = file_hash(git_repo, "agent/prompt.py")
    (git_repo / "agent" / "prompt.py").write_text("# changed\n")
    r2 = file_hash(git_repo, "agent/prompt.py")
    assert r1.sha != r2.sha


# ---------------------------------------------------------------------------
# apply_and_commit
# ---------------------------------------------------------------------------

def test_apply_and_commit_basic(git_repo) -> None:
    from _git_ratchet import apply_and_commit, current_commit_sha

    base_sha = current_commit_sha(git_repo).sha
    result = apply_and_commit(
        git_repo,
        "agent/prompt.py",
        b"# improved content\n",
        "karpathy: improve system prompt",
    )
    assert result.ok, result.error
    assert result.base_sha == base_sha
    assert result.commit_sha != base_sha
    assert len(result.commit_sha) == 40

    # File on disk has new content.
    content = (git_repo / "agent" / "prompt.py").read_bytes()
    assert content == b"# improved content\n"


def test_apply_and_commit_only_one_file(git_repo) -> None:
    """Only the target file should appear in the commit."""
    from _git_ratchet import apply_and_commit

    result = apply_and_commit(
        git_repo,
        "agent/prompt.py",
        b"# v2\n",
        "test: apply only one file",
    )
    assert result.ok, result.error

    # Check the commit touched only agent/prompt.py.
    r = subprocess.run(
        ["git", "-C", str(git_repo), "diff-tree", "--no-commit-id", "-r",
         "--name-only", result.commit_sha],
        capture_output=True, text=True,
    )
    changed_files = r.stdout.strip().splitlines()
    assert changed_files == ["agent/prompt.py"]


def test_apply_and_commit_refuses_other_staged(git_repo) -> None:
    """If other staged changes exist, apply_and_commit must refuse."""
    from _git_ratchet import apply_and_commit

    # Stage another file manually.
    other = git_repo / "other.py"
    other.write_text("# other\n")
    _git(["add", "other.py"], git_repo)

    result = apply_and_commit(
        git_repo, "agent/prompt.py", b"# blocked\n", "should not commit"
    )
    assert not result.ok
    assert "staged" in result.error.lower()


def test_apply_and_commit_non_git_dir(tmp_path) -> None:
    from _git_ratchet import apply_and_commit
    bad = tmp_path / "notgit"
    bad.mkdir()
    result = apply_and_commit(bad, "f.py", b"x", "msg")
    assert not result.ok


# ---------------------------------------------------------------------------
# revert_commit
# ---------------------------------------------------------------------------

def test_revert_commit_restores_content(git_repo) -> None:
    from _git_ratchet import apply_and_commit, revert_commit

    original = (git_repo / "agent" / "prompt.py").read_bytes()

    apply_result = apply_and_commit(
        git_repo, "agent/prompt.py", b"# experiment\n", "apply experiment"
    )
    assert apply_result.ok, apply_result.error

    revert_result = revert_commit(
        git_repo, apply_result.commit_sha, "revert: experiment"
    )
    assert revert_result.ok, revert_result.error
    assert len(revert_result.revert_sha) == 40
    assert revert_result.revert_sha != apply_result.commit_sha

    content_after_revert = (git_repo / "agent" / "prompt.py").read_bytes()
    assert content_after_revert == original


def test_revert_commit_non_git_dir(tmp_path) -> None:
    from _git_ratchet import revert_commit
    bad = tmp_path / "notgit"
    bad.mkdir()
    result = revert_commit(bad, "deadbeef" * 5, "msg")
    assert not result.ok


# ---------------------------------------------------------------------------
# detect_manual_conflict
# ---------------------------------------------------------------------------

def test_detect_manual_conflict_no_conflict(git_repo) -> None:
    from _git_ratchet import blob_sha, detect_manual_conflict

    expected = blob_sha(git_repo, "agent/prompt.py").sha
    result = detect_manual_conflict(git_repo, "agent/prompt.py", expected)
    assert result.ok
    assert result.conflict is False


def test_detect_manual_conflict_detects_out_of_band_edit(git_repo) -> None:
    from _git_ratchet import apply_and_commit, blob_sha, detect_manual_conflict

    # Capture blob sha after apply.
    apply_result = apply_and_commit(
        git_repo, "agent/prompt.py", b"# experiment\n", "apply"
    )
    assert apply_result.ok
    applied_blob = blob_sha(git_repo, "agent/prompt.py").sha

    # Simulate an out-of-band manual edit (written but NOT committed).
    (git_repo / "agent" / "prompt.py").write_text("# manual edit!\n")

    # Now manually stage + commit it to change HEAD blob.
    _git(["add", "agent/prompt.py"], git_repo)
    _git(["commit", "-m", "manual edit"], git_repo)

    result = detect_manual_conflict(git_repo, "agent/prompt.py", applied_blob)
    assert result.ok
    assert result.conflict is True


def test_detect_manual_conflict_non_git_dir(tmp_path) -> None:
    from _git_ratchet import detect_manual_conflict
    bad = tmp_path / "notgit"
    bad.mkdir()
    result = detect_manual_conflict(bad, "f.py", "abc123")
    assert not result.ok


# ---------------------------------------------------------------------------
# _assert_profile_root — default-home widening (PR #137 fix)
# ---------------------------------------------------------------------------

def test_assert_profile_root_accepts_default_home(tmp_path, monkeypatch) -> None:
    """_assert_profile_root must accept a root equal to _DEFAULT_HOME."""
    import _git_ratchet
    default_home = tmp_path / "hermes_home"
    default_home.mkdir()
    monkeypatch.setattr(_git_ratchet, "_DEFAULT_HOME", default_home)
    # Patch _PROFILES_ROOT to something unrelated so only _DEFAULT_HOME matches.
    monkeypatch.setattr(_git_ratchet, "_PROFILES_ROOT", tmp_path / "profiles")
    # Should not raise.
    _git_ratchet._assert_profile_root(default_home)


def test_assert_profile_root_accepts_named_profile_subtree(tmp_path, monkeypatch) -> None:
    """_assert_profile_root must accept a root that is a subtree of _PROFILES_ROOT."""
    import _git_ratchet
    profiles_root = tmp_path / "profiles"
    named = profiles_root / "coder"
    named.mkdir(parents=True)
    monkeypatch.setattr(_git_ratchet, "_PROFILES_ROOT", profiles_root)
    monkeypatch.setattr(_git_ratchet, "_DEFAULT_HOME", tmp_path / "hermes_home")
    # Should not raise.
    _git_ratchet._assert_profile_root(named)


def test_assert_profile_root_rejects_arbitrary_path(tmp_path, monkeypatch) -> None:
    """_assert_profile_root must reject a path that is neither _DEFAULT_HOME nor under _PROFILES_ROOT."""
    import _git_ratchet
    monkeypatch.setattr(_git_ratchet, "_PROFILES_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(_git_ratchet, "_DEFAULT_HOME", tmp_path / "hermes_home")
    outside = tmp_path / "random_dir"
    outside.mkdir()
    with pytest.raises(ValueError, match="not inside"):
        _git_ratchet._assert_profile_root(outside)


def test_assert_contained_still_rejects_traversal_inside_default_home(tmp_path, monkeypatch) -> None:
    """_assert_contained must still reject a relpath that escapes default home."""
    import _git_ratchet
    default_home = tmp_path / "hermes_home"
    default_home.mkdir()
    monkeypatch.setattr(_git_ratchet, "_DEFAULT_HOME", default_home)
    monkeypatch.setattr(_git_ratchet, "_PROFILES_ROOT", tmp_path / "profiles")
    # Traversal relpath escaping default_home — must raise even though root is valid.
    with pytest.raises(ValueError, match="traversal"):
        _git_ratchet._assert_contained(default_home, "../../etc/passwd")
