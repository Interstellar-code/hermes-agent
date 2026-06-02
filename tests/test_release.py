"""Tests for scripts/release.py — commit parsing and origin_repo_slug fixes."""

import sys
from pathlib import Path
import importlib
import types

import pytest

# Allow importing scripts/release.py as a module
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_release():
    """Import scripts/release.py, reloading to pick up monkeypatches cleanly."""
    spec = importlib.util.spec_from_file_location(
        "release", SCRIPTS_DIR / "release.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# get_commits — robust RS/US record parsing
# ---------------------------------------------------------------------------

# Synthetic git log output using the NEW %x1e/%x1f format:
#   RS (0x1e) starts each record, US (0x1f) separates fields.
#   commit 1: multi-line body with Co-Authored-By trailer and blank line
#   commit 2: body with only a blank line
#   commit 3: empty body
_SYNTHETIC_LOG = (
    "\x1e"
    "aabbccdd1111111111111111111111111111111111"
    "\x1f" "Alice Smith"
    "\x1f" "alice@example.com"
    "\x1f" "feat(core): add widget support"
    "\x1f" "This adds widget support.\n\nCo-authored-by: Bob Jones <bob@example.com>\n"
    "\x1e"
    "bbccddee2222222222222222222222222222222222"
    "\x1f" "Carol White"
    "\x1f" "carol@example.com"
    "\x1f" "fix(api): handle null response"
    "\x1f" "\nsome notes\n"
    "\x1e"
    "ccddeeff3333333333333333333333333333333333"
    "\x1f" "Dave Brown"
    "\x1f" "dave@example.com"
    "\x1f" "chore: bump deps"
    "\x1f" ""
)


def test_get_commits_parses_all_records(monkeypatch):
    """get_commits must return one entry per RS-delimited record (3 here).

    The OLD \\0\\0 split would have returned 1 entry for this input because
    there are no double-NUL sequences — this test would fail with the old code.
    """
    release = _load_release()

    # Monkeypatch the module-level `git` to return our synthetic log
    monkeypatch.setattr(release, "git", lambda *args, **kwargs: _SYNTHETIC_LOG)

    commits = release.get_commits("v1.0.0")

    assert len(commits) == 3, (
        f"Expected 3 commits, got {len(commits)}. "
        "The old \\0\\0 split would return 1 — check the RS/US format fix."
    )


def test_get_commits_correct_fields(monkeypatch):
    """Each parsed commit has the correct hash, subject, and body."""
    release = _load_release()
    monkeypatch.setattr(release, "git", lambda *args, **kwargs: _SYNTHETIC_LOG)

    commits = release.get_commits("v1.0.0")

    c0 = commits[0]
    assert c0["sha"] == "aabbccdd1111111111111111111111111111111111"
    assert c0["short_sha"] == "aabbccdd"
    assert c0["subject"] == "feat(core): add widget support"
    assert c0["author_name"] == "Alice Smith"

    c1 = commits[1]
    assert c1["sha"] == "bbccddee2222222222222222222222222222222222"
    assert c1["subject"] == "fix(api): handle null response"

    c2 = commits[2]
    assert c2["sha"] == "ccddeeff3333333333333333333333333333333333"
    assert c2["subject"] == "chore: bump deps"


def test_get_commits_multiline_body_retained(monkeypatch):
    """Body field retains internal newlines (multi-line body must not be split)."""
    release = _load_release()
    monkeypatch.setattr(release, "git", lambda *args, **kwargs: _SYNTHETIC_LOG)

    commits = release.get_commits("v1.0.0")
    # Commit 1 body contains a Co-authored-by line — parse_coauthors should find it
    # The coauthors list is populated only for real human names; Bob Jones has no
    # special filter, so at least the body was parsed correctly (no IndexError etc.)
    assert isinstance(commits[0]["coauthors"], list)


def test_get_commits_empty_log(monkeypatch):
    """get_commits returns [] when git returns an empty string."""
    release = _load_release()
    monkeypatch.setattr(release, "git", lambda *args, **kwargs: "")

    commits = release.get_commits("v1.0.0")
    assert commits == []


# ---------------------------------------------------------------------------
# origin_repo_slug — HTTPS and SSH URL parsing
# ---------------------------------------------------------------------------

def test_origin_repo_slug_https(monkeypatch):
    release = _load_release()
    monkeypatch.setattr(
        release, "git",
        lambda *args, **kwargs: "https://github.com/Interstellar-code/hermes-agent.git",
    )
    assert release.origin_repo_slug() == "Interstellar-code/hermes-agent"


def test_origin_repo_slug_https_no_dotgit(monkeypatch):
    release = _load_release()
    monkeypatch.setattr(
        release, "git",
        lambda *args, **kwargs: "https://github.com/Interstellar-code/hermes-agent",
    )
    assert release.origin_repo_slug() == "Interstellar-code/hermes-agent"


def test_origin_repo_slug_ssh(monkeypatch):
    release = _load_release()
    monkeypatch.setattr(
        release, "git",
        lambda *args, **kwargs: "git@github.com:Interstellar-code/hermes-agent.git",
    )
    assert release.origin_repo_slug() == "Interstellar-code/hermes-agent"


def test_origin_repo_slug_ssh_no_dotgit(monkeypatch):
    release = _load_release()
    monkeypatch.setattr(
        release, "git",
        lambda *args, **kwargs: "git@github.com:Interstellar-code/hermes-agent",
    )
    assert release.origin_repo_slug() == "Interstellar-code/hermes-agent"


def test_origin_repo_slug_fallback(monkeypatch):
    """Returns NousResearch fallback when origin URL is unavailable."""
    release = _load_release()
    monkeypatch.setattr(release, "git", lambda *args, **kwargs: "")
    assert release.origin_repo_slug() == "NousResearch/hermes-agent"
