"""Tests for scripts/release.py commit parsing and repo slug handling."""

from pathlib import Path
import importlib.util


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_release():
    spec = importlib.util.spec_from_file_location("release", SCRIPTS_DIR / "release.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_SYNTHETIC_LOG = (
    "\x1e"
    "aabbccdd1111111111111111111111111111111111"
    "\x1fAlice Smith"
    "\x1falice@example.com"
    "\x1ffeat(core): add widget support"
    "\x1fThis adds widget support.\n\nCo-authored-by: Bob Jones <bob@example.com>\n"
    "\x1e"
    "bbccddee2222222222222222222222222222222222"
    "\x1fCarol White"
    "\x1fcarol@example.com"
    "\x1ffix(api): handle null response"
    "\x1f\nsome notes\n"
    "\x1e"
    "ccddeeff3333333333333333333333333333333333"
    "\x1fDave Brown"
    "\x1fdave@example.com"
    "\x1fchore: bump deps"
    "\x1f"
)


def test_get_commits_parses_all_records(monkeypatch):
    release = _load_release()
    monkeypatch.setattr(release, "git", lambda *args, **kwargs: _SYNTHETIC_LOG)
    assert len(release.get_commits("v1.0.0")) == 3


def test_get_commits_correct_fields(monkeypatch):
    release = _load_release()
    monkeypatch.setattr(release, "git", lambda *args, **kwargs: _SYNTHETIC_LOG)
    commits = release.get_commits("v1.0.0")
    assert commits[0]["sha"] == "aabbccdd1111111111111111111111111111111111"
    assert commits[0]["short_sha"] == "aabbccdd"
    assert commits[0]["subject"] == "feat(core): add widget support"
    assert commits[1]["subject"] == "fix(api): handle null response"
    assert commits[2]["subject"] == "chore: bump deps"


def test_get_commits_multiline_body_retained(monkeypatch):
    release = _load_release()
    monkeypatch.setattr(release, "git", lambda *args, **kwargs: _SYNTHETIC_LOG)
    commits = release.get_commits("v1.0.0")
    assert isinstance(commits[0]["coauthors"], list)


def test_get_commits_empty_log(monkeypatch):
    release = _load_release()
    monkeypatch.setattr(release, "git", lambda *args, **kwargs: "")
    assert release.get_commits("v1.0.0") == []


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
    release = _load_release()
    monkeypatch.setattr(release, "git", lambda *args, **kwargs: "")
    assert release.origin_repo_slug() == "NousResearch/hermes-agent"
