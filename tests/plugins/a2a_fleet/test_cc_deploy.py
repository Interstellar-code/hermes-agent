"""Unit tests for a2a_fleet.cc_deploy (deploy_cc_receiver + companions).

No real process launch, no network: the detached launch, health-check, claude
probe, and PID-alive checks are stubbed via monkeypatch so the suite is fully
deterministic and hermetic. Stdlib + pytest only.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from a2a_fleet import cc_deploy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_repo(tmp_path: Path, *, git: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    if git:
        (repo / ".git").mkdir()
    return repo


@pytest.fixture
def stub_template(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the template lookup at a fake cc_receiver.py so copyfile works."""
    fake_pkg_dir = tmp_path / "fake_pkg"
    (fake_pkg_dir / "templates").mkdir(parents=True)
    template = fake_pkg_dir / "templates" / cc_deploy.RECEIVER_FILENAME
    template.write_text("# fake receiver template\nprint('hi')\n")
    monkeypatch.setattr(cc_deploy, "__file__", str(fake_pkg_dir / "cc_deploy.py"))
    return template


@pytest.fixture
def stub_runtime(monkeypatch: pytest.MonkeyPatch):
    """Stub launch / health / claude-probe / stop-old so no real process/network."""
    launched = {}

    def fake_launch(repo, receiver_path, log_path):
        launched["repo"] = repo
        launched["receiver"] = receiver_path
        launched["log"] = log_path
        return 4242

    monkeypatch.setattr(cc_deploy, "_launch_receiver", fake_launch)
    monkeypatch.setattr(cc_deploy, "_poll_health", lambda port, budget_s=8.0: True)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: True)
    monkeypatch.setattr(cc_deploy, "_stop_old_receiver", lambda pid_path: None)
    return launched


# ---------------------------------------------------------------------------
# Path canonicalization
# ---------------------------------------------------------------------------

def test_canonicalize_rejects_empty():
    path, err = cc_deploy.canonicalize_repo_path("")
    assert path is None
    assert "empty" in err


def test_canonicalize_rejects_nonexistent(tmp_path: Path):
    path, err = cc_deploy.canonicalize_repo_path(str(tmp_path / "nope"))
    assert path is None
    assert "does not exist" in err


def test_canonicalize_rejects_non_dir(tmp_path: Path):
    f = tmp_path / "afile"
    f.write_text("x")
    path, err = cc_deploy.canonicalize_repo_path(str(f))
    assert path is None
    assert "not a directory" in err


def test_canonicalize_rejects_symlink_escape(tmp_path: Path):
    real = tmp_path / "real_repo"
    real.mkdir()
    link = tmp_path / "link_repo"
    link.symlink_to(real, target_is_directory=True)
    path, err = cc_deploy.canonicalize_repo_path(str(link))
    assert path is None
    assert "canonical" in err


def test_canonicalize_accepts_real_dir(tmp_path: Path):
    repo = _make_repo(tmp_path)
    path, err = cc_deploy.canonicalize_repo_path(str(repo))
    assert err is None
    assert path == Path(os.path.realpath(str(repo)))


# ---------------------------------------------------------------------------
# A2A.md role file
# ---------------------------------------------------------------------------

def test_a2a_md_written_with_role_text(tmp_path: Path, stub_template, stub_runtime):
    repo = _make_repo(tmp_path)
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert res.get("deployed") is True
    a2a = repo / ".hermes" / "A2A.md"
    text = a2a.read_text()
    assert "Claude Code executor peer" in text
    assert "http://127.0.0.1:9219" in text
    assert "contextId" in text


# ---------------------------------------------------------------------------
# CLAUDE.md @import managed block
# ---------------------------------------------------------------------------

def test_claude_md_created_when_absent(tmp_path: Path):
    claude = tmp_path / "CLAUDE.md"
    status = cc_deploy.upsert_claude_md_import(claude)
    assert status == "imported"
    content = claude.read_text()
    assert cc_deploy.CLAUDE_MD_START in content
    assert cc_deploy.CLAUDE_MD_IMPORT_LINE in content
    assert cc_deploy.CLAUDE_MD_END in content


def test_claude_md_idempotent_single_block(tmp_path: Path):
    claude = tmp_path / "CLAUDE.md"
    cc_deploy.upsert_claude_md_import(claude)
    cc_deploy.upsert_claude_md_import(claude)
    content = claude.read_text()
    assert content.count(cc_deploy.CLAUDE_MD_START) == 1
    assert content.count(cc_deploy.CLAUDE_MD_END) == 1
    assert content.count(cc_deploy.CLAUDE_MD_IMPORT_LINE) == 1


def test_claude_md_preserves_existing_content(tmp_path: Path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# My project rules\nDo the thing.\n")
    cc_deploy.upsert_claude_md_import(claude)
    content = claude.read_text()
    assert "# My project rules" in content
    assert "Do the thing." in content
    assert cc_deploy.CLAUDE_MD_START in content


def test_claude_md_replaces_stale_block_in_place(tmp_path: Path):
    claude = tmp_path / "CLAUDE.md"
    stale = (
        "# Header\n\n"
        f"{cc_deploy.CLAUDE_MD_START}\n"
        "@.hermes/OLD_STALE.md\n"
        f"{cc_deploy.CLAUDE_MD_END}\n\n"
        "# Footer\n"
    )
    claude.write_text(stale)
    status = cc_deploy.upsert_claude_md_import(claude)
    content = claude.read_text()
    assert status == "refreshed"
    assert "OLD_STALE" not in content
    assert cc_deploy.CLAUDE_MD_IMPORT_LINE in content
    assert content.count(cc_deploy.CLAUDE_MD_START) == 1
    # Surrounding content untouched.
    assert "# Header" in content
    assert "# Footer" in content


def test_claude_md_already_imported_noop(tmp_path: Path):
    claude = tmp_path / "CLAUDE.md"
    cc_deploy.upsert_claude_md_import(claude)
    status = cc_deploy.upsert_claude_md_import(claude)
    assert status == "already-imported"


# ---------------------------------------------------------------------------
# a2a_receiver.json — keys match cc_receiver.load_config
# ---------------------------------------------------------------------------

def test_receiver_config_keys_and_cwd_pinned(tmp_path: Path):
    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    cfg = cc_deploy.build_receiver_config(canonical, 9300, "sonnet")
    # cwd pinned to canonical path
    assert cfg["repo_path"] == str(canonical)
    # keys the template's load_config / DEFAULTS expect
    assert cfg["bind_host"] == "127.0.0.1"
    assert cfg["bind_port"] == 9300
    assert cfg["hermes_url"] == "http://127.0.0.1:9219/jsonrpc"
    assert cfg["role_file"] == ".hermes/A2A.md"
    assert cfg["claude_model"] == "sonnet"
    assert isinstance(cfg["idle_timeout_s"], int)
    assert isinstance(cfg["max_concurrent_turns"], int)


def test_receiver_config_omits_model_when_none(tmp_path: Path):
    repo = _make_repo(tmp_path)
    cfg = cc_deploy.build_receiver_config(Path(os.path.realpath(str(repo))), 9300, None)
    assert "claude_model" not in cfg


def test_receiver_config_matches_template_default_keys():
    """Every key we write must be a key the template's DEFAULTS recognizes."""
    from a2a_fleet.templates import cc_receiver

    cfg = cc_deploy.build_receiver_config(Path("/tmp/x"), 9300, "opus")
    template_keys = set(cc_receiver.DEFAULTS.keys())
    for key in cfg:
        assert key in template_keys, f"{key} not understood by cc_receiver.DEFAULTS"


def test_deploy_writes_config_with_pinned_cwd(tmp_path: Path, stub_template, stub_runtime):
    repo = _make_repo(tmp_path)
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo), bind_port=9311, model="opus"))
    assert res["deployed"] is True
    cfg_path = repo / ".hermes" / "a2a_receiver.json"
    cfg = json.loads(cfg_path.read_text())
    assert cfg["repo_path"] == str(Path(os.path.realpath(str(repo))))
    assert cfg["bind_port"] == 9311
    assert cfg["claude_model"] == "opus"
    # No leftover temp file (atomic write).
    assert not (repo / ".hermes" / "a2a_receiver.json.tmp").exists()


# ---------------------------------------------------------------------------
# Template copy
# ---------------------------------------------------------------------------

def test_template_copied_into_hermes_dir(tmp_path: Path, stub_template, stub_runtime):
    repo = _make_repo(tmp_path)
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert res["deployed"] is True
    dest = repo / ".hermes" / "cc_receiver.py"
    assert dest.exists()
    assert dest.read_text() == stub_template.read_text()


# ---------------------------------------------------------------------------
# Stop-old before relaunch
# ---------------------------------------------------------------------------

def test_stop_old_receiver_stops_live_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pid_path = tmp_path / "cc_receiver.pid"
    pid_path.write_text("1234")
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: True)
    terminated = {}
    monkeypatch.setattr(cc_deploy, "_terminate_pid", lambda pid: terminated.setdefault("pid", pid) or True)
    stopped = cc_deploy._stop_old_receiver(pid_path)
    assert stopped == 1234
    assert terminated["pid"] == 1234
    assert not pid_path.exists()


def test_stop_old_receiver_none_when_dead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pid_path = tmp_path / "cc_receiver.pid"
    pid_path.write_text("1234")
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: False)
    assert cc_deploy._stop_old_receiver(pid_path) is None


def test_deploy_stops_old_before_relaunch(tmp_path: Path, stub_template, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    # A live receiver already recorded.
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir()
    (hermes_dir / "cc_receiver.pid").write_text("999")
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: True)
    killed = {}
    monkeypatch.setattr(cc_deploy, "_terminate_pid", lambda pid: killed.setdefault("pid", pid) or True)
    monkeypatch.setattr(cc_deploy, "_launch_receiver", lambda repo, rp, lp: 5555)
    monkeypatch.setattr(cc_deploy, "_poll_health", lambda port, budget_s=8.0: True)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: True)
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert killed["pid"] == 999
    assert res["pid"] == 5555
    assert any("stopped previous receiver" in w for w in res["warnings"])


# ---------------------------------------------------------------------------
# status / stop handlers
# ---------------------------------------------------------------------------

def test_status_running_requires_pid_and_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir()
    (hermes_dir / "cc_receiver.pid").write_text("321")
    (hermes_dir / "a2a_receiver.json").write_text(json.dumps({"bind_port": 9300}))
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cc_deploy, "_check_health_once", lambda port: True)
    res = _run(cc_deploy.cc_receiver_status_handler(str(repo)))
    assert res["running"] is True
    assert res["pid"] == 321
    assert res["port"] == 9300
    assert res["healthy"] is True


def test_status_not_running_when_pid_alive_but_unhealthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir()
    (hermes_dir / "cc_receiver.pid").write_text("321")
    (hermes_dir / "a2a_receiver.json").write_text(json.dumps({"bind_port": 9300}))
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cc_deploy, "_check_health_once", lambda port: False)
    res = _run(cc_deploy.cc_receiver_status_handler(str(repo)))
    assert res["running"] is False
    assert res["healthy"] is False


def test_status_no_pidfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    (repo / ".hermes").mkdir()
    monkeypatch.setattr(cc_deploy, "_check_health_once", lambda port: False)
    res = _run(cc_deploy.cc_receiver_status_handler(str(repo)))
    assert res["running"] is False
    assert res["pid"] is None


def test_stop_handler_terminates_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir()
    pid_path = hermes_dir / "cc_receiver.pid"
    pid_path.write_text("777")
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cc_deploy, "_terminate_pid", lambda pid: True)
    res = _run(cc_deploy.cc_receiver_stop_handler(str(repo)))
    assert res["stopped"] is True
    assert res["pid"] == 777
    assert not pid_path.exists()


def test_stop_handler_no_pidfile(tmp_path: Path):
    repo = _make_repo(tmp_path)
    (repo / ".hermes").mkdir()
    res = _run(cc_deploy.cc_receiver_stop_handler(str(repo)))
    assert res["stopped"] is False
    assert res["pid"] is None


def test_stop_handler_process_already_dead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir()
    pid_path = hermes_dir / "cc_receiver.pid"
    pid_path.write_text("888")
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: False)
    res = _run(cc_deploy.cc_receiver_stop_handler(str(repo)))
    assert res["stopped"] is False
    assert res["pid"] == 888
    assert not pid_path.exists()


# ---------------------------------------------------------------------------
# Error paths — handlers return {"error": ...}, never raise
# ---------------------------------------------------------------------------

def test_deploy_error_on_missing_repo(tmp_path: Path):
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(tmp_path / "ghost")))
    assert "error" in res
    assert "does not exist" in res["error"]
    assert "deployed" not in res


def test_deploy_error_on_missing_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    # Point __file__ at a dir with no templates/cc_receiver.py.
    empty = tmp_path / "empty_pkg"
    empty.mkdir()
    monkeypatch.setattr(cc_deploy, "__file__", str(empty / "cc_deploy.py"))
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert "error" in res
    assert "template missing" in res["error"]


def test_deploy_error_on_launch_failure(tmp_path: Path, stub_template, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)

    def boom(repo, rp, lp):
        raise OSError("address already in use")

    monkeypatch.setattr(cc_deploy, "_launch_receiver", boom)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: True)
    monkeypatch.setattr(cc_deploy, "_stop_old_receiver", lambda pid_path: None)
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert "error" in res
    assert "launch" in res["error"]


def test_deploy_unhealthy_status_when_health_fails(tmp_path: Path, stub_template, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(cc_deploy, "_launch_receiver", lambda repo, rp, lp: 1010)
    monkeypatch.setattr(cc_deploy, "_poll_health", lambda port, budget_s=8.0: False)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: True)
    monkeypatch.setattr(cc_deploy, "_stop_old_receiver", lambda pid_path: None)
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert res["deployed"] is True
    assert res["status"] == "unhealthy"
    assert any("health-check failed" in w for w in res["warnings"])


def test_deploy_warns_on_non_git_repo(tmp_path: Path, stub_template, stub_runtime):
    repo = _make_repo(tmp_path, git=False)
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert res["deployed"] is True
    assert any("git repo" in w for w in res["warnings"])


def test_deploy_warns_when_claude_missing(tmp_path: Path, stub_template, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(cc_deploy, "_launch_receiver", lambda repo, rp, lp: 1212)
    monkeypatch.setattr(cc_deploy, "_poll_health", lambda port, budget_s=8.0: True)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: False)
    monkeypatch.setattr(cc_deploy, "_stop_old_receiver", lambda pid_path: None)
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert any("claude CLI not found" in w for w in res["warnings"])


def test_status_error_on_bad_repo(tmp_path: Path):
    res = _run(cc_deploy.cc_receiver_status_handler(str(tmp_path / "nope")))
    assert "error" in res


def test_stop_error_on_bad_repo(tmp_path: Path):
    res = _run(cc_deploy.cc_receiver_stop_handler(str(tmp_path / "nope")))
    assert "error" in res
