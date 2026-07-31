"""Strict updater preconditions, against real temporary git repositories (#200).

These drive actual `git` rather than mocks. The whole value of strict mode is
that it refuses states a browser must never update through, and a mocked git
would only prove that the mock agrees with the code.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import strict_update


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo: Path, name: str, body: str = "x") -> str:
    (repo / name).write_text(body, encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", f"add {name}")
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path: Path):
    """An upstream and a clone of it, both on `main`."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _run(upstream, "init", "--initial-branch=main")
    _run(upstream, "config", "user.email", "t@example.com")
    _run(upstream, "config", "user.name", "T")
    _commit(upstream, "base.txt")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(upstream), str(clone)], check=True, capture_output=True
    )
    _run(clone, "config", "user.email", "t@example.com")
    _run(clone, "config", "user.name", "T")
    return upstream, clone


# ---------------------------------------------------------------------------
# The safety guarantee itself
# ---------------------------------------------------------------------------


def test_no_destructive_verb_is_reachable() -> None:
    """The acceptance criterion, as a property of the allowlist.

    Strict mode can only become destructive by someone adding a verb to
    _ALLOWED_GIT, which this test makes a deliberate act rather than an
    accident buried in a diff.
    """
    overlap = set(strict_update._ALLOWED_GIT) & set(strict_update.FORBIDDEN_GIT)
    assert overlap == set(), f"destructive git verbs reachable in strict mode: {overlap}"


def test_git_chokepoint_refuses_a_forbidden_verb(repos) -> None:
    """Even a direct caller cannot get a forbidden verb to git."""
    _upstream, clone = repos
    for verb in ("stash", "reset", "clean", "rebase", "checkout"):
        with pytest.raises(strict_update.StrictUpdateError) as exc:
            strict_update._git(clone, verb, "--hard")
        assert "refuses git subcommand" in str(exc.value)


def test_merge_is_only_ever_ff_only(repos) -> None:
    """`merge` is allowlisted, so pin that it is only used with --ff-only."""
    source = Path(strict_update.__file__).read_text(encoding="utf-8")
    merge_calls = [ln for ln in source.splitlines() if '"merge"' in ln and "_git(" in ln]
    assert merge_calls, "expected a merge call to pin"
    for line in merge_calls:
        assert '"--ff-only"' in line, f"merge without --ff-only: {line.strip()}"


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------


def test_clean_behind_is_the_applicable_state(repos) -> None:
    upstream, clone = repos
    target = _commit(upstream, "new.txt")
    _run(clone, "fetch", "origin", "main")

    result = strict_update.preflight(clone)
    assert result["checkout_state"] == "clean-behind"
    assert result["can_apply_strict"] is True
    assert result["target_head"] == target
    assert result["behind"] == 1
    assert result["strict_update_api"] == strict_update.STRICT_UPDATE_API


def test_up_to_date_is_not_applicable(repos) -> None:
    _upstream, clone = repos
    result = strict_update.preflight(clone)
    assert result["checkout_state"] == "clean-uptodate"
    assert result["can_apply_strict"] is False


@pytest.mark.parametrize("kind", ["tracked", "untracked"])
def test_dirty_checkout_is_refused_and_files_are_basenames(repos, kind) -> None:
    """Untracked counts as dirty — that is the case stash exists to paper over."""
    upstream, clone = repos
    _commit(upstream, "new.txt")
    _run(clone, "fetch", "origin", "main")

    if kind == "tracked":
        (clone / "base.txt").write_text("locally modified", encoding="utf-8")
    else:
        (clone / "my_notes.txt").write_text("scratch", encoding="utf-8")

    result = strict_update.preflight(clone)
    assert result["checkout_state"] == "dirty"
    assert result["can_apply_strict"] is False
    assert result["blocking_files"], "the operator needs to know what is blocking"
    for name in result["blocking_files"]:
        assert "/" not in name, "absolute paths must not leak to a browser"


def test_blocking_files_are_bounded(repos) -> None:
    _upstream, clone = repos
    for i in range(strict_update._MAX_BLOCKING_FILES + 15):
        (clone / f"junk_{i}.txt").write_text("x", encoding="utf-8")

    result = strict_update.preflight(clone)
    assert len(result["blocking_files"]) == strict_update._MAX_BLOCKING_FILES
    assert result["blocking_files_truncated"] is True


def test_ahead_checkout_is_refused(repos) -> None:
    _upstream, clone = repos
    _commit(clone, "local_work.txt")
    result = strict_update.preflight(clone)
    assert result["checkout_state"] == "ahead"
    assert result["can_apply_strict"] is False


def test_diverged_checkout_is_refused(repos) -> None:
    upstream, clone = repos
    _commit(upstream, "theirs.txt")
    _commit(clone, "mine.txt")
    _run(clone, "fetch", "origin", "main")

    result = strict_update.preflight(clone)
    assert result["checkout_state"] == "diverged"
    assert result["can_apply_strict"] is False


def test_detached_head_is_refused(repos) -> None:
    _upstream, clone = repos
    head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    _run(clone, "checkout", "--detach", head)

    result = strict_update.preflight(clone)
    assert result["checkout_state"] == "detached"
    assert result["can_apply_strict"] is False


def test_unsupported_branch_is_refused(repos) -> None:
    _upstream, clone = repos
    _run(clone, "checkout", "-b", "feature/side-quest")
    result = strict_update.preflight(clone)
    assert result["checkout_state"] == "unsupported-branch"
    assert result["can_apply_strict"] is False


def test_missing_remote_is_refused(repos) -> None:
    _upstream, clone = repos
    _run(clone, "remote", "remove", "origin")
    result = strict_update.preflight(clone)
    assert result["checkout_state"] == "wrong-remote"
    assert result["can_apply_strict"] is False


def test_non_git_directory_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = strict_update.preflight(plain)
    assert result["checkout_state"] == "not-a-git-checkout"
    assert result["can_apply_strict"] is False


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def test_apply_fast_forwards_a_clean_behind_checkout(repos) -> None:
    upstream, clone = repos
    target = _commit(upstream, "shipped.txt")

    result = strict_update.apply_strict(clone)
    assert result["ok"] is True
    assert result["source_advanced"] is True
    assert result["current_head"] == target
    assert (clone / "shipped.txt").exists()


def test_apply_preserves_local_work_instead_of_stashing_it(repos) -> None:
    """The reason this module exists: a browser click must not move your work."""
    upstream, clone = repos
    _commit(upstream, "theirs.txt")
    (clone / "precious.txt").write_text("uncommitted work", encoding="utf-8")

    result = strict_update.apply_strict(clone)

    assert result["ok"] is False
    assert result["error"] == "not_applicable"
    assert result["checkout_state"] == "dirty"
    assert (clone / "precious.txt").read_text(encoding="utf-8") == "uncommitted work"
    stashes = subprocess.run(
        ["git", "-C", str(clone), "stash", "list"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert stashes == "", "strict mode must never create a stash"


def test_stale_local_head_assertion_is_rejected(repos) -> None:
    upstream, clone = repos
    _commit(upstream, "theirs.txt")

    result = strict_update.apply_strict(
        clone, expected_current_head="0" * 40
    )
    assert result["ok"] is False
    assert result["error"] == "stale_confirmation"


def test_target_moving_between_check_and_apply_is_rejected(repos) -> None:
    """A moved upstream must not be applied silently — the core assertion."""
    upstream, clone = repos
    confirmed_target = _commit(upstream, "confirmed.txt")
    _commit(upstream, "sneaked_in_later.txt")

    result = strict_update.apply_strict(
        clone, expected_target_head=confirmed_target
    )
    assert result["ok"] is False
    assert result["error"] == "target_moved"
    assert not (clone / "sneaked_in_later.txt").exists()
    assert not (clone / "confirmed.txt").exists(), "nothing may be applied"


def test_matching_assertions_apply_cleanly(repos) -> None:
    upstream, clone = repos
    head_before = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    target = _commit(upstream, "agreed.txt")

    result = strict_update.apply_strict(
        clone,
        expected_current_head=head_before,
        expected_target_head=target,
    )
    assert result["ok"] is True
    assert result["current_head"] == target


# ---------------------------------------------------------------------------
# Endpoint wiring (#200): mode routing, concurrency, server-owned inputs
# ---------------------------------------------------------------------------


class _FakePayload:
    def __init__(self, mode=None, cur=None, tgt=None):
        self.mode = mode
        self.expected_current_head = cur
        self.expected_target_head = tgt


def test_strict_refuses_non_git_installs(monkeypatch) -> None:
    """pip/docker/nix have no fast-forward; refusing beats pretending."""
    import hermes_cli.web_server as web_server

    monkeypatch.setattr(web_server, "_dashboard_local_update_managed_externally", lambda: False)
    monkeypatch.setattr(web_server, "detect_install_method", lambda _root: "pip")

    result = web_server._apply_strict_update(_FakePayload(mode="strict"))
    assert result["ok"] is False
    assert result["error"] == "strict_unsupported_install"


def test_concurrent_strict_update_is_409(monkeypatch) -> None:
    import hermes_cli.web_server as web_server
    from fastapi import HTTPException

    monkeypatch.setattr(web_server, "_dashboard_local_update_managed_externally", lambda: False)
    monkeypatch.setattr(web_server, "detect_install_method", lambda _root: "git")
    monkeypatch.setattr(web_server, "_strict_update_in_flight", lambda: True)

    with pytest.raises(HTTPException) as exc:
        web_server._apply_strict_update(_FakePayload(mode="strict"))
    assert exc.value.status_code == 409


def test_branch_is_server_owned_and_falls_back_safely(monkeypatch) -> None:
    """A client must never choose which branch gets installed."""
    import hermes_cli.web_server as web_server

    monkeypatch.setattr(web_server, "load_config", lambda: {"update": {"strict_branch": "release"}})
    assert web_server._strict_update_branch() == "release"

    monkeypatch.setattr(web_server, "load_config", lambda: {})
    assert web_server._strict_update_branch() == strict_update.DEFAULT_BRANCH

    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(web_server, "load_config", _boom)
    assert web_server._strict_update_branch() == strict_update.DEFAULT_BRANCH


def test_successful_strict_apply_flags_restart_required(monkeypatch, repos) -> None:
    """After the source moves, this process is stale — the #199 condition."""
    import hermes_cli.web_server as web_server

    upstream, clone = repos
    _commit(upstream, "shipped.txt")

    monkeypatch.setattr(web_server, "_dashboard_local_update_managed_externally", lambda: False)
    monkeypatch.setattr(web_server, "detect_install_method", lambda _root: "git")
    monkeypatch.setattr(web_server, "_strict_update_in_flight", lambda: False)
    monkeypatch.setattr(web_server, "PROJECT_ROOT", clone)
    monkeypatch.setattr(web_server, "_record_completed_action", lambda *a, **k: None)
    # MUST be stubbed: the real one spawns a detached `hermes update
    # --refresh-deps --restart-after-refresh`, which installs dependencies
    # into the live venv and restarts every gateway on the machine running
    # the suite.
    monkeypatch.setattr(web_server, "_spawn_hermes_action", _FakeProc.spawn)

    result = web_server._apply_strict_update(_FakePayload(mode="strict"))
    assert result["ok"] is True
    assert result["restart_required"] is True
    assert result["mode"] == "strict"


class _FakeProc:
    """Stand-in for the detached action process, plus the call it recorded."""

    calls: list = []
    pid = 4242

    @classmethod
    def spawn(cls, subcommand, name):
        cls.calls.append((list(subcommand), name))
        return cls()


def test_applied_source_hands_off_to_the_refresh_action(monkeypatch, repos) -> None:
    """The applied source is not runnable until deps and assets catch up.

    Strict apply only fast-forwards git. The dependency/build/restart
    lifecycle runs in a DETACHED fresh interpreter, because this dashboard is
    served from the very process that is now stale — it cannot install into
    its own running venv and then restart itself.
    """
    import hermes_cli.web_server as web_server

    upstream, clone = repos
    _commit(upstream, "shipped.txt")
    _FakeProc.calls.clear()

    monkeypatch.setattr(web_server, "_dashboard_local_update_managed_externally", lambda: False)
    monkeypatch.setattr(web_server, "detect_install_method", lambda _root: "git")
    monkeypatch.setattr(web_server, "_strict_update_in_flight", lambda: False)
    monkeypatch.setattr(web_server, "PROJECT_ROOT", clone)
    monkeypatch.setattr(web_server, "_spawn_hermes_action", _FakeProc.spawn)

    result = web_server._apply_strict_update(_FakePayload(mode="strict"))

    assert _FakeProc.calls == [
        (["update", "--refresh-deps", "--restart-after-refresh"], "hermes-update")
    ]
    assert result["action"] == "hermes-update"
    assert result["pid"] == _FakeProc.pid
    assert result["restart_required"] is True


def test_refresh_that_cannot_start_is_reported_not_swallowed(monkeypatch, repos) -> None:
    """A source update whose follow-up never launched is a half-done update.

    The fast-forward already landed, so ok stays true — but the caller is told
    the refresh did not start, rather than being left to assume it is running.
    """
    import hermes_cli.web_server as web_server

    upstream, clone = repos
    _commit(upstream, "shipped.txt")

    def _boom(subcommand, name):
        raise OSError("no interpreter")

    monkeypatch.setattr(web_server, "_dashboard_local_update_managed_externally", lambda: False)
    monkeypatch.setattr(web_server, "detect_install_method", lambda _root: "git")
    monkeypatch.setattr(web_server, "_strict_update_in_flight", lambda: False)
    monkeypatch.setattr(web_server, "PROJECT_ROOT", clone)
    monkeypatch.setattr(web_server, "_spawn_hermes_action", _boom)

    result = web_server._apply_strict_update(_FakePayload(mode="strict"))

    assert result["ok"] is True
    assert result["restart_required"] is True
    assert "no interpreter" in result["refresh_error"]
    assert "could not start" in result["post_update"]
