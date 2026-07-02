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

    monkeypatch.setattr(cc_deploy, "_launch_receiver",
                        lambda repo, receiver_path, log_path, env=None: fake_launch(
                            repo, receiver_path, log_path))
    monkeypatch.setattr(cc_deploy, "_poll_health",
                        lambda port, budget_s=8.0, expected_repo_path=None: True)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: True)
    monkeypatch.setattr(cc_deploy, "_stop_old_receiver", lambda pid_path: (None, None))
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


def test_canonicalize_resolves_symlink_to_real_target(tmp_path: Path):
    # A symlinked path (e.g. macOS /tmp -> /private/tmp, /Volumes mounts) is
    # RESOLVED to its real on-disk target and accepted — not rejected. Security
    # is preserved because the receiver's cwd is pinned to the real path.
    real = tmp_path / "real_repo"
    real.mkdir()
    link = tmp_path / "link_repo"
    link.symlink_to(real, target_is_directory=True)
    path, err = cc_deploy.canonicalize_repo_path(str(link))
    assert err is None
    assert path == Path(os.path.realpath(str(real)))


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


def test_role_text_includes_handshake_protocol():
    """The role text must teach a fresh ``claude -p`` how to answer a handshake.

    A handshake message (reserved contextId ``handshake:<repo-slug>``) must elicit
    a structured confirmation: role=executor, the repo cwd it is operating in, a
    brief harness inventory (skills / MCP / CLAUDE.md), and ready/not-ready.
    """
    text = cc_deploy.A2A_ROLE_TEXT
    low = text.lower()
    assert "handshake" in low
    # Must instruct the confirmation contents.
    assert "executor" in low
    assert "cwd" in low or "working directory" in low
    assert "harness" in low or "skills" in low
    assert "ready" in low


def test_role_text_includes_session_and_guardrail_instructions():
    """Role text must convey same-contextId continuity + concise-reply guardrails."""
    text = cc_deploy.A2A_ROLE_TEXT
    low = text.lower()
    assert "contextid" in low
    # Same context_id = continuing session.
    assert "same" in low and "session" in low
    # Concise replies with results/status (so Hermes can summarize to the user).
    assert "concise" in low
    assert "status" in low


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
    stopped, err = cc_deploy._stop_old_receiver(pid_path)
    assert stopped == 1234
    assert err is None
    assert terminated["pid"] == 1234
    assert not pid_path.exists()


def test_stop_old_receiver_none_when_dead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pid_path = tmp_path / "cc_receiver.pid"
    pid_path.write_text("1234")
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: False)
    assert cc_deploy._stop_old_receiver(pid_path) == (None, None)


def test_stop_old_receiver_aborts_when_terminate_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fail-closed: if the old receiver won't die, return an error AND keep the pidfile."""
    pid_path = tmp_path / "cc_receiver.pid"
    pid_path.write_text("4321")
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cc_deploy, "_terminate_pid", lambda pid: False)  # survives
    stopped, err = cc_deploy._stop_old_receiver(pid_path)
    assert stopped == 4321
    assert err is not None and "could not stop existing receiver" in err
    # Pidfile preserved (we did NOT confirm the process dead).
    assert pid_path.exists()


def test_deploy_stops_old_before_relaunch(tmp_path: Path, stub_template, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    # A live receiver already recorded.
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir()
    (hermes_dir / "cc_receiver.pid").write_text("999")
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: True)
    killed = {}
    monkeypatch.setattr(cc_deploy, "_terminate_pid", lambda pid: killed.setdefault("pid", pid) or True)
    monkeypatch.setattr(cc_deploy, "_launch_receiver", lambda repo, rp, lp, env=None: 5555)
    monkeypatch.setattr(cc_deploy, "_poll_health",
                        lambda port, budget_s=8.0, expected_repo_path=None: True)
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
    monkeypatch.setattr(cc_deploy, "_check_health_once",
                        lambda port, expected_repo_path=None: True)
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
    monkeypatch.setattr(cc_deploy, "_check_health_once",
                        lambda port, expected_repo_path=None: False)
    res = _run(cc_deploy.cc_receiver_status_handler(str(repo)))
    assert res["running"] is False
    assert res["healthy"] is False


def test_status_no_pidfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    (repo / ".hermes").mkdir()
    monkeypatch.setattr(cc_deploy, "_check_health_once",
                        lambda port, expected_repo_path=None: False)
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

    def boom(repo, rp, lp, env=None):
        raise OSError("address already in use")

    monkeypatch.setattr(cc_deploy, "_launch_receiver", boom)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: True)
    monkeypatch.setattr(cc_deploy, "_stop_old_receiver", lambda pid_path: (None, None))
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert "error" in res
    assert "launch" in res["error"]


def test_deploy_errors_and_kills_child_when_health_fails(tmp_path: Path, stub_template, monkeypatch: pytest.MonkeyPatch):
    """Health never coming up must NOT report success: error + child torn down."""
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(cc_deploy, "_launch_receiver", lambda repo, rp, lp, env=None: 1010)
    monkeypatch.setattr(cc_deploy, "_poll_health",
                        lambda port, budget_s=8.0, expected_repo_path=None: False)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: True)
    monkeypatch.setattr(cc_deploy, "_stop_old_receiver", lambda pid_path: (None, None))
    killed = {}
    monkeypatch.setattr(cc_deploy, "_kill_launched_child",
                        lambda pid: killed.setdefault("pid", pid))
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert "error" in res
    assert "never became healthy" in res["error"]
    assert "deployed" not in res
    # The just-launched child was torn down.
    assert killed["pid"] == 1010


def test_deploy_warns_on_non_git_repo(tmp_path: Path, stub_template, stub_runtime):
    repo = _make_repo(tmp_path, git=False)
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert res["deployed"] is True
    assert any("git repo" in w for w in res["warnings"])


def test_deploy_warns_when_claude_missing(tmp_path: Path, stub_template, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(cc_deploy, "_launch_receiver", lambda repo, rp, lp, env=None: 1212)
    monkeypatch.setattr(cc_deploy, "_poll_health",
                        lambda port, budget_s=8.0, expected_repo_path=None: True)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: False)
    monkeypatch.setattr(cc_deploy, "_stop_old_receiver", lambda pid_path: (None, None))
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert any("claude CLI not found" in w for w in res["warnings"])


def test_status_error_on_bad_repo(tmp_path: Path):
    res = _run(cc_deploy.cc_receiver_status_handler(str(tmp_path / "nope")))
    assert "error" in res


def test_stop_error_on_bad_repo(tmp_path: Path):
    res = _run(cc_deploy.cc_receiver_stop_handler(str(tmp_path / "nope")))
    assert "error" in res


# ---------------------------------------------------------------------------
# Inbound auth provisioning (security)
# ---------------------------------------------------------------------------

def test_deploy_provisions_inbound_token(tmp_path: Path, stub_template, monkeypatch: pytest.MonkeyPatch):
    """A random inbound token is generated, written to config as auth_token_env,
    injected into the child env, and surfaced in the result."""
    repo = _make_repo(tmp_path)
    captured = {}

    def fake_launch(repo_, rp, lp, env=None):
        captured["env"] = env
        return 2020

    monkeypatch.setattr(cc_deploy, "_launch_receiver", fake_launch)
    monkeypatch.setattr(cc_deploy, "_poll_health",
                        lambda port, budget_s=8.0, expected_repo_path=None: True)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: True)
    monkeypatch.setattr(cc_deploy, "_stop_old_receiver", lambda pid_path: (None, None))

    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert res["deployed"] is True
    # #98: the detached receiver's env always pins HERMES_HOME so it resolves the
    # deployer's profile, not the silent ~/.hermes default-profile fallback.
    assert captured["env"] is not None
    assert "HERMES_HOME" in captured["env"]
    # Returned to Hermes for fleet_send wiring.
    token = res["receiver_token"]
    token_env = res["receiver_token_env"]
    assert token and isinstance(token, str)
    assert token_env.startswith(cc_deploy.RECEIVER_TOKEN_ENV_PREFIX)
    # Config records the env var NAME (never the literal token).
    cfg = json.loads((repo / ".hermes" / "a2a_receiver.json").read_text())
    assert cfg["auth_token_env"] == token_env
    assert token not in json.dumps(cfg)
    # Child env carries the actual token under that name.
    assert captured["env"] is not None
    assert captured["env"][token_env] == token


def test_stable_token_env_name_is_deterministic(tmp_path: Path):
    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    name1 = cc_deploy.stable_token_env_name(canonical)
    name2 = cc_deploy.stable_token_env_name(canonical)
    # Stable across calls for the same repo.
    assert name1 == name2
    assert name1.startswith(cc_deploy.RECEIVER_TOKEN_ENV_PREFIX)
    # Env-var-safe: only uppercase / digits / underscore.
    assert all(c.isupper() or c.isdigit() or c == "_" for c in name1)


def test_stable_token_env_name_differs_per_repo(tmp_path: Path):
    a = tmp_path / "repo_a"
    a.mkdir()
    b = tmp_path / "repo_b"
    b.mkdir()
    na = cc_deploy.stable_token_env_name(Path(os.path.realpath(str(a))))
    nb = cc_deploy.stable_token_env_name(Path(os.path.realpath(str(b))))
    assert na != nb


def test_deploy_uses_stable_token_name_and_publishes_to_environ(
    tmp_path: Path, stub_template, monkeypatch: pytest.MonkeyPatch
):
    """The inbound token env var NAME is the stable per-repo name, and the VALUE
    is published into the gateway's os.environ this session (for in-process
    fleet_send) while the NAME stays constant across redeploys."""
    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    expected_name = cc_deploy.stable_token_env_name(canonical)
    monkeypatch.delenv(expected_name, raising=False)

    monkeypatch.setattr(cc_deploy, "_launch_receiver", lambda r, rp, lp, env=None: 2121)
    monkeypatch.setattr(cc_deploy, "_poll_health",
                        lambda port, budget_s=8.0, expected_repo_path=None: True)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: True)
    monkeypatch.setattr(cc_deploy, "_stop_old_receiver", lambda pid_path: (None, None))

    res1 = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert res1["receiver_token_env"] == expected_name
    # Published into the current process environment under the stable name.
    assert os.environ[expected_name] == res1["receiver_token"]

    res2 = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    # NAME stable across redeploys; VALUE fresh.
    assert res2["receiver_token_env"] == expected_name
    assert res2["receiver_token"] != res1["receiver_token"]
    assert os.environ[expected_name] == res2["receiver_token"]


def test_deploy_no_auth_opt_out(tmp_path: Path, stub_template, monkeypatch: pytest.MonkeyPatch):
    """no_auth=True skips token provisioning (open loopback dev)."""
    repo = _make_repo(tmp_path)
    captured = {}

    def fake_launch(repo_, rp, lp, env=None):
        captured["env"] = env
        return 3030

    monkeypatch.setattr(cc_deploy, "_launch_receiver", fake_launch)
    monkeypatch.setattr(cc_deploy, "_poll_health",
                        lambda port, budget_s=8.0, expected_repo_path=None: True)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: True)
    monkeypatch.setattr(cc_deploy, "_stop_old_receiver", lambda pid_path: (None, None))

    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo), no_auth=True))
    assert res["deployed"] is True
    assert "receiver_token" not in res
    cfg = json.loads((repo / ".hermes" / "a2a_receiver.json").read_text())
    assert "auth_token_env" not in cfg
    # no_auth opt-out is enforced by the absence of receiver_token (result) and
    # auth_token_env (config), asserted above. #98: the child env is still built
    # to pin HERMES_HOME even on the no_auth path.
    assert captured["env"] is not None
    assert "HERMES_HOME" in captured["env"]
    assert any("no_auth" in w for w in res["warnings"])


def test_deploy_passes_hermes_auth_token_env_to_config(tmp_path: Path):
    """hermes_auth_token_env flows through build_receiver_config into the config."""
    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    cfg = cc_deploy.build_receiver_config(
        canonical, 9300, "sonnet", hermes_auth_token_env="HERMES_CB_TOKEN"
    )
    assert cfg["hermes_auth_token_env"] == "HERMES_CB_TOKEN"
    # Omitted when empty.
    cfg2 = cc_deploy.build_receiver_config(canonical, 9300, "sonnet")
    assert "hermes_auth_token_env" not in cfg2


# ---------------------------------------------------------------------------
# Health-check identity (repo_path echoed by /health must match the target)
# ---------------------------------------------------------------------------

def test_check_health_identity_match(monkeypatch: pytest.MonkeyPatch):
    class _Resp:
        status = 200

        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        return _Resp(json.dumps({"ok": True, "repo_path": "/canon/repo"}).encode())

    monkeypatch.setattr(cc_deploy.urllib.request, "urlopen", fake_urlopen)
    # Matching repo_path -> healthy.
    assert cc_deploy._check_health_once(9300, expected_repo_path="/canon/repo") is True
    # Mismatched repo_path (stale/unrelated process on the port) -> unhealthy.
    assert cc_deploy._check_health_once(9300, expected_repo_path="/other/repo") is False
    # No expectation -> ok flag alone suffices (back-compat).
    assert cc_deploy._check_health_once(9300) is True


def test_deploy_aborts_when_old_receiver_wont_die(tmp_path: Path, stub_template, monkeypatch: pytest.MonkeyPatch):
    """Fail-closed at the handler level: stop-old failure aborts the deploy and the
    new receiver is never launched."""
    repo = _make_repo(tmp_path)
    launched = {"n": 0}

    def fake_launch(repo_, rp, lp, env=None):
        launched["n"] += 1
        return 4040

    monkeypatch.setattr(cc_deploy, "_launch_receiver", fake_launch)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: True)
    monkeypatch.setattr(
        cc_deploy, "_stop_old_receiver",
        lambda pid_path: (999, "could not stop existing receiver (pid 999); aborting redeploy"),
    )
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert "error" in res
    assert "could not stop existing receiver" in res["error"]
    assert "deployed" not in res
    assert launched["n"] == 0, "must NOT launch a second receiver after stop-old failure"


# ---------------------------------------------------------------------------
# Boot-reconcile (Phase 3) — selects managed claude_code peers, decides
# reconcile-vs-leave, never spawns real processes / hits the network in tests.
# ---------------------------------------------------------------------------

def _stub_fleet(monkeypatch: pytest.MonkeyPatch, agents: dict):
    from a2a_fleet import fleet_config
    monkeypatch.setattr(fleet_config, "load_fleet", lambda profile=None: {"agents": agents})


def test_iter_supported_managed_peers_selection():
    """iter_supported_managed_peers covers all supported modes (claude_code + opencode)."""
    from a2a_fleet.managed_peers import iter_supported_managed_peers

    agents = {
        # plain url peer — not managed
        "plain": {"url": "http://x", "managed": False, "mode": None, "repo_path": None},
        # managed but mode=None (legacy / Route B — no mode field) — excluded
        "legacy-no-mode": {"url": "http://y", "managed": True, "mode": None, "repo_path": "/r"},
        # managed but unknown mode — excluded
        "unknown-mode": {"url": "http://z", "managed": True, "mode": "llm", "repo_path": "/r"},
        # managed claude_code but no repo_path — excluded
        "no-repo": {"url": "http://w", "managed": True, "mode": "claude_code", "repo_path": None},
        # valid claude_code managed peer — included
        "good-cc": {"url": "http://c", "managed": True, "mode": "claude_code", "repo_path": "/r"},
        # valid opencode managed peer — included
        "good-oc": {"url": "http://d", "managed": True, "mode": "opencode", "repo_path": "/r2"},
    }
    selected = list(iter_supported_managed_peers(agents))
    names = [n for n, _ in selected]
    assert "good-cc" in names
    assert "good-oc" in names
    assert "plain" not in names
    assert "legacy-no-mode" not in names
    assert "unknown-mode" not in names
    assert "no-repo" not in names
    assert len(names) == 2


def test_reconcile_noop_when_no_managed_peers(monkeypatch: pytest.MonkeyPatch):
    _stub_fleet(monkeypatch, {
        "construct": {"url": "http://x", "managed": False, "mode": None, "repo_path": None},
    })
    # Deploy must never be called when there is nothing managed to reconcile.
    monkeypatch.setattr(
        cc_deploy, "deploy_cc_receiver_handler",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deploy")),
    )
    assert cc_deploy.reconcile_managed_receivers() == []


def test_reconcile_leaves_healthy_peer_with_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    token_env = cc_deploy.stable_token_env_name(canonical)
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir()
    (hermes_dir / "cc_receiver.pid").write_text("4242")
    (hermes_dir / "a2a_receiver.json").write_text(json.dumps({"bind_port": 9300}))
    monkeypatch.setenv(token_env, "live-token")
    _stub_fleet(monkeypatch, {
        "claude-code": {
            "url": "http://127.0.0.1:9300", "managed": True,
            "mode": "claude_code", "repo_path": str(canonical), "token_env": token_env,
        },
    })
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cc_deploy, "_check_health_once",
                        lambda port, expected_repo_path=None: True)
    # Healthy + token present -> must NOT redeploy.
    monkeypatch.setattr(
        cc_deploy, "deploy_cc_receiver_handler",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deploy")),
    )
    rows = cc_deploy.reconcile_managed_receivers()
    assert len(rows) == 1
    assert rows[0]["action"] == "healthy"


def test_reconcile_redeploys_when_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    _stub_fleet(monkeypatch, {
        "claude-code": {
            "url": "http://127.0.0.1:9301", "managed": True,
            "mode": "claude_code", "repo_path": str(canonical),
        },
    })
    # No pidfile / not healthy -> down.
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(cc_deploy, "_check_health_once",
                        lambda port, expected_repo_path=None: False)
    called = {}

    async def fake_deploy(repo_path, bind_port=cc_deploy.DEFAULT_BIND_PORT, **k):
        called["repo_path"] = repo_path
        called["bind_port"] = bind_port
        return {"deployed": True, "pid": 7777}

    monkeypatch.setattr(cc_deploy, "deploy_cc_receiver_handler", fake_deploy)
    rows = cc_deploy.reconcile_managed_receivers()
    assert rows[0]["action"] == "redeployed"
    assert rows[0]["pid"] == 7777
    assert called["repo_path"] == str(canonical)
    # Port for the redeploy comes from the fleet.yaml peer url, not on-disk json.
    assert called["bind_port"] == 9301


def test_reconcile_leaves_healthy_and_republishes_persisted_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """H1: a HEALTHY receiver is LEFT running even if this gateway lost the token
    in os.environ after a restart — the persisted .token is re-published instead of
    killing the (possibly mid-task) executor."""
    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    token_env = cc_deploy.stable_token_env_name(canonical)
    monkeypatch.delenv(token_env, raising=False)  # gateway lost the token on restart
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir()
    (hermes_dir / "cc_receiver.pid").write_text("4242")
    (hermes_dir / "a2a_receiver.json").write_text(json.dumps({"bind_port": 9300}))
    # The token persisted by the prior successful deploy.
    (hermes_dir / cc_deploy.TOKEN_FILENAME).write_text("persisted-token")
    _stub_fleet(monkeypatch, {
        "claude-code": {
            "url": "http://127.0.0.1:9300", "managed": True,
            "mode": "claude_code", "repo_path": str(canonical), "token_env": token_env,
        },
    })
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cc_deploy, "_check_health_once",
                        lambda port, expected_repo_path=None: True)
    # Healthy -> must NOT redeploy (would kill the in-flight executor).
    monkeypatch.setattr(
        cc_deploy, "deploy_cc_receiver_handler",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deploy a healthy receiver")),
    )
    rows = cc_deploy.reconcile_managed_receivers()
    assert rows[0]["action"] == "healthy"
    # The persisted token was re-published so in-session fleet_send keeps working.
    assert os.environ[token_env] == "persisted-token"


def test_reconcile_failed_deploy_surfaces_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    _stub_fleet(monkeypatch, {
        "claude-code": {
            "url": "http://127.0.0.1:9300", "managed": True,
            "mode": "claude_code", "repo_path": str(canonical),
        },
    })
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(cc_deploy, "_check_health_once",
                        lambda port, expected_repo_path=None: False)

    async def fake_deploy(repo_path, bind_port=cc_deploy.DEFAULT_BIND_PORT, **k):
        return {"error": "port in use"}

    monkeypatch.setattr(cc_deploy, "deploy_cc_receiver_handler", fake_deploy)
    rows = cc_deploy.reconcile_managed_receivers()
    assert rows[0]["action"] == "failed"
    assert "port in use" in rows[0]["error"]


def test_reconcile_never_raises_on_bad_fleet(monkeypatch: pytest.MonkeyPatch):
    from a2a_fleet import fleet_config

    def boom(profile=None):
        raise fleet_config.FleetConfigError("no fleet.yaml")

    monkeypatch.setattr(fleet_config, "load_fleet", boom)
    # Must swallow the error and return an empty summary, not raise.
    assert cc_deploy.reconcile_managed_receivers() == []


def test_reconcile_redeploy_port_from_url_over_on_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When the on-disk a2a_receiver.json port drifts from fleet.yaml, the redeploy
    uses the fleet.yaml (desired-state) port."""
    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir()
    # On-disk says 9300; fleet.yaml url says 9305 -> prefer fleet.yaml.
    (hermes_dir / "a2a_receiver.json").write_text(json.dumps({"bind_port": 9300}))
    _stub_fleet(monkeypatch, {
        "claude-code": {
            "url": "http://127.0.0.1:9305", "managed": True,
            "mode": "claude_code", "repo_path": str(canonical),
        },
    })
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(cc_deploy, "_check_health_once",
                        lambda port, expected_repo_path=None: False)
    called = {}

    async def fake_deploy(repo_path, bind_port=cc_deploy.DEFAULT_BIND_PORT, **k):
        called["bind_port"] = bind_port
        return {"deployed": True, "pid": 6060}

    monkeypatch.setattr(cc_deploy, "deploy_cc_receiver_handler", fake_deploy)
    rows = cc_deploy.reconcile_managed_receivers()
    assert rows[0]["action"] == "redeployed"
    assert called["bind_port"] == 9305


# ---------------------------------------------------------------------------
# Token persistence (.token 0600) + .gitignore (hardening)
# ---------------------------------------------------------------------------

def test_deploy_persists_token_file_0600(tmp_path: Path, stub_template, monkeypatch: pytest.MonkeyPatch):
    """A successful deploy writes <repo>/.hermes/.token (mode 0600) with the token."""
    import stat

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(cc_deploy, "_launch_receiver", lambda r, rp, lp, env=None: 1357)
    monkeypatch.setattr(cc_deploy, "_poll_health",
                        lambda port, budget_s=8.0, expected_repo_path=None: True)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: True)
    monkeypatch.setattr(cc_deploy, "_stop_old_receiver", lambda pid_path: (None, None))

    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert res["deployed"] is True
    token_path = repo / ".hermes" / cc_deploy.TOKEN_FILENAME
    assert token_path.exists()
    assert token_path.read_text() == res["receiver_token"]
    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_deploy_writes_gitignore_with_runtime_entries(tmp_path: Path, stub_template, stub_runtime):
    repo = _make_repo(tmp_path)
    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert res["deployed"] is True
    gitignore = repo / ".hermes" / cc_deploy.GITIGNORE_FILENAME
    assert gitignore.exists()
    body = gitignore.read_text()
    for entry in (".token", "*.pid", "*.log", "a2a-inbox*", "a2a-transcript*", "a2a-inbox.offset"):
        assert entry in body, f"{entry} missing from .hermes/.gitignore"


def test_gitignore_idempotent_and_preserves_user_lines(tmp_path: Path):
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    gi = hermes_dir / cc_deploy.GITIGNORE_FILENAME
    gi.write_text("# user content\nmy-secret-dir/\n")
    cc_deploy.upsert_hermes_gitignore(gi)
    cc_deploy.upsert_hermes_gitignore(gi)  # second call must not duplicate
    body = gi.read_text()
    assert "# user content" in body
    assert "my-secret-dir/" in body
    assert body.count(".token") == 1
    assert body.count("a2a-inbox.offset") == 1


def test_deploy_no_token_leak_on_health_fail(tmp_path: Path, stub_template, monkeypatch: pytest.MonkeyPatch):
    """Health-fail path: os.environ is NOT mutated and no .token is written."""
    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    token_env = cc_deploy.stable_token_env_name(canonical)
    monkeypatch.delenv(token_env, raising=False)

    monkeypatch.setattr(cc_deploy, "_launch_receiver", lambda r, rp, lp, env=None: 2468)
    monkeypatch.setattr(cc_deploy, "_poll_health",
                        lambda port, budget_s=8.0, expected_repo_path=None: False)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: True)
    monkeypatch.setattr(cc_deploy, "_stop_old_receiver", lambda pid_path: (None, None))
    monkeypatch.setattr(cc_deploy, "_kill_launched_child", lambda pid: None)

    res = _run(cc_deploy.deploy_cc_receiver_handler(str(repo)))
    assert "error" in res
    assert "never became healthy" in res["error"]
    # No env leak: the token was never published to the parent process.
    assert token_env not in os.environ
    # No persisted secret.
    assert not (repo / ".hermes" / cc_deploy.TOKEN_FILENAME).exists()


def test_stable_token_env_name_suffix_is_12_hex(tmp_path: Path):
    """The SHA-256 suffix is 12 hex chars (widened from 8 for entropy)."""
    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    name = cc_deploy.stable_token_env_name(canonical)
    # Trailing component after the final underscore is the hash suffix.
    suffix = name.rsplit("_", 1)[-1]
    assert len(suffix) == 12
    assert all(c in "0123456789ABCDEF" for c in suffix)


# ---------------------------------------------------------------------------
# Reconcile singleton guard (#6) — repeated starts don't double-spawn.
# ---------------------------------------------------------------------------

def test_reconcile_in_thread_singleton(monkeypatch: pytest.MonkeyPatch):
    """Calling the in_thread starter twice while a reconcile thread is alive must
    NOT spawn a second thread."""
    import threading

    # Reset module state for isolation.
    monkeypatch.setattr(cc_deploy, "_reconcile_thread", None)

    spawned = {"n": 0}
    release = threading.Event()

    def slow_reconcile():
        spawned["n"] += 1
        release.wait(timeout=2.0)
        return []

    monkeypatch.setattr(cc_deploy, "reconcile_managed_receivers", slow_reconcile)
    cc_deploy.reconcile_managed_receivers_in_thread()
    # Second call while the first worker is still running -> no-op.
    cc_deploy.reconcile_managed_receivers_in_thread()
    release.set()
    t = cc_deploy._reconcile_thread
    if t is not None:
        t.join(timeout=2.0)
    assert spawned["n"] == 1


# ---------------------------------------------------------------------------
# Gateway dispatch injects task_id via **kwargs — handlers must absorb it
# ---------------------------------------------------------------------------

def test_handlers_absorb_injected_task_id(tmp_path: Path):
    """registry.dispatch() calls handler(args, task_id=...). The cc handlers
    must tolerate the injected task_id (and any future injected kwargs) rather
    than raising TypeError before their own logic runs."""
    repo = _make_repo(tmp_path)
    # status/stop are side-effect-light (no live process) — they must not raise
    # TypeError on the injected kwarg; they return a normal result dict.
    res_status = _run(cc_deploy.cc_receiver_status_handler(str(repo), task_id="t-1"))
    assert isinstance(res_status, dict) and "running" in res_status
    res_stop = _run(cc_deploy.cc_receiver_stop_handler(str(repo), task_id="t-1"))
    assert isinstance(res_stop, dict)


# ---------------------------------------------------------------------------
# _terminate_pid must not false-negative on a just-SIGKILLed (zombie) process
# ---------------------------------------------------------------------------

def test_terminate_pid_reports_success_after_sigkill_even_if_zombie(monkeypatch):
    """SIGKILL is uncatchable; a lingering zombie (os.kill(pid,0) still 'alive')
    must NOT make _terminate_pid return False (the smoke-test false-negative)."""
    events = []
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: True)  # always 'alive' (zombie)
    monkeypatch.setattr(cc_deploy, "STOP_TERM_WAIT_S", 0.05)
    monkeypatch.setattr(cc_deploy, "STOP_POLL_INTERVAL_S", 0.01)

    class FakeProc:
        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")

    monkeypatch.setattr(cc_deploy.psutil, "Process", lambda pid: FakeProc())
    out = cc_deploy._terminate_pid(4242)
    assert out is True
    assert events == ["terminate", "kill"]


def test_terminate_pid_true_when_process_already_gone(monkeypatch):
    def fake_kill(pid, sig):
        raise ProcessLookupError
    monkeypatch.setattr(cc_deploy.os, "kill", fake_kill)
    assert cc_deploy._terminate_pid(4242) is True


# ---------------------------------------------------------------------------
# Tool handler return value MUST reach the wire as a string, not a dict
# (openai-compatible upstreams reject object content → 400 fallback_exhausted)
# ---------------------------------------------------------------------------

def test_json_tool_result_stringifies_dict_returns():
    import asyncio
    import json as _json
    import a2a_fleet as _pkg

    async def returns_dict(**kwargs):
        return {"running": True, "pid": 123, "port": 9311}

    async def returns_str(**kwargs):
        return "plain"

    wrapped_dict = _pkg._json_tool_result(returns_dict)
    wrapped_str = _pkg._json_tool_result(returns_str)
    out_dict = asyncio.run(wrapped_dict(task_id="t"))
    out_str = asyncio.run(wrapped_str())
    assert isinstance(out_dict, str)
    assert _json.loads(out_dict) == {"running": True, "pid": 123, "port": 9311}
    assert out_str == "plain"  # already-string returns pass through unchanged


def test_canonicalize_unwraps_nested_repo_path_dict(tmp_path):
    # Weaker models nest the arg as {"repo_path": "..."} — must be unwrapped.
    repo = _make_repo(tmp_path)
    p, err = cc_deploy.canonicalize_repo_path({"repo_path": str(repo)})
    assert err is None
    assert p == Path(os.path.realpath(str(repo)))


# ---------------------------------------------------------------------------
# Boot-reconcile: opencode managed peer + legacy no-mode peer
# ---------------------------------------------------------------------------

def test_reconcile_down_opencode_peer_triggers_oc_redeploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A managed opencode peer that is down must trigger oc_deploy (not cc_deploy)."""
    from a2a_fleet import oc_deploy

    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    _stub_fleet(monkeypatch, {
        "opencode": {
            "url": "http://127.0.0.1:9310", "managed": True,
            "mode": "opencode", "repo_path": str(canonical),
        },
    })
    # Down (pid file absent, not healthy).
    monkeypatch.setattr(cc_deploy, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(cc_deploy, "_check_health_once",
                        lambda port, expected_repo_path=None: False)
    called = {}

    async def fake_oc_deploy(repo_path, bind_port=oc_deploy.DEFAULT_BIND_PORT, **k):
        called["repo_path"] = repo_path
        called["bind_port"] = bind_port
        called["mode"] = "opencode"
        return {"deployed": True, "pid": 9999}

    monkeypatch.setattr(oc_deploy, "deploy_oc_receiver_handler", fake_oc_deploy)
    rows = cc_deploy.reconcile_managed_receivers()
    assert len(rows) == 1
    assert rows[0]["action"] == "redeployed"
    assert rows[0]["pid"] == 9999
    assert called.get("mode") == "opencode", "oc_deploy was not called for mode=opencode peer"
    assert called["repo_path"] == str(canonical)
    assert called["bind_port"] == 9310


def test_reconcile_legacy_no_mode_peer_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A managed peer with mode=None (legacy / Route B) must be ignored gracefully, not crash."""
    _stub_fleet(monkeypatch, {
        "legacy": {
            "url": "http://127.0.0.1:9300", "managed": True,
            "mode": None, "repo_path": str(tmp_path),
        },
    })
    # If the legacy peer were processed it might call _managed_receiver_module which raises.
    rows = cc_deploy.reconcile_managed_receivers()
    # No crash, empty result (unsupported mode is filtered by iter_supported_managed_peers).
    assert rows == []


def test_reconcile_managed_receiver_module_selected_for_opencode() -> None:
    """_managed_receiver_module('opencode') returns the oc_deploy module."""
    from a2a_fleet import oc_deploy as oc_mod

    module = cc_deploy._managed_receiver_module("opencode")
    assert module is oc_mod


def test_deploy_cc_handler_dict_dispatch_extracts_all_params(
    tmp_path: Path, stub_template: Path, stub_runtime
) -> None:
    """Registry calls handler(args_dict, task_id=...) — all params must be unwrapped for cc too.

    This test FAILS before the cc_deploy dict-unwrap fix (bind_port silently defaults)
    and PASSES after.
    """
    repo = _make_repo(tmp_path)
    res = _run(cc_deploy.deploy_cc_receiver_handler(
        {"repo_path": str(repo), "bind_port": 9312, "model": "claude-test", "no_auth": True},
        task_id="t-cc",
    ))
    assert res.get("deployed") is True, f"deploy failed: {res}"
    cfg_path = repo / ".hermes" / "a2a_receiver.json"
    cfg = json.loads(cfg_path.read_text())
    assert cfg["bind_port"] == 9312, (
        f"Expected bind_port=9312 (from dict args), got {cfg['bind_port']} — "
        "dict-unwrap for non-repo_path params is missing in cc_deploy"
    )
    assert cfg.get("claude_model") == "claude-test"
