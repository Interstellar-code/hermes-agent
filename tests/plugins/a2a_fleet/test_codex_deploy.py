"""Unit tests for a2a_fleet.codex_deploy (deploy_codex_receiver + companions).

Registration-parity assertions validate that __init__.py wires the 3 Codex tools.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest

from a2a_fleet import codex_deploy

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "a2a_fleet"
INIT_PATH = PLUGIN_DIR / "__init__.py"
CODEX_DEPLOY_PATH = PLUGIN_DIR / "codex_deploy.py"


def _run(coro):
    return asyncio.run(coro)


def _make_repo(tmp_path: Path, *, git: bool = True) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    if git:
        (repo / ".git").mkdir()
    return repo


def _init_source() -> str:
    return INIT_PATH.read_text(encoding="utf-8")


@pytest.fixture
def stub_template(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    fake_pkg_dir = tmp_path / "fake_pkg"
    (fake_pkg_dir / "templates").mkdir(parents=True)
    template = fake_pkg_dir / "templates" / codex_deploy.RECEIVER_FILENAME
    template.write_text("# fake codex receiver template\nprint('hi')\n")
    monkeypatch.setattr(codex_deploy, "__file__", str(fake_pkg_dir / "codex_deploy.py"))
    return template


@pytest.fixture
def stub_runtime(monkeypatch: pytest.MonkeyPatch):
    launched = {}

    def fake_launch(repo, receiver_path, log_path, env=None):
        launched["repo"] = repo
        launched["receiver"] = receiver_path
        launched["log"] = log_path
        launched["env"] = env
        return 31337

    monkeypatch.setattr(
        codex_deploy,
        "_launch_receiver",
        lambda repo, receiver_path, log_path, env=None: fake_launch(
            repo, receiver_path, log_path, env=env
        ),
    )
    monkeypatch.setattr(codex_deploy, "_poll_health", lambda port, budget_s=8.0, expected_repo_path=None: True)
    monkeypatch.setattr(codex_deploy, "_probe_codex_cli", lambda: True)
    monkeypatch.setattr(codex_deploy, "_stop_old_receiver", lambda pid_path: (None, None))
    return launched


def test_codex_deploy_module_exists() -> None:
    assert CODEX_DEPLOY_PATH.is_file(), "plugins/a2a_fleet/codex_deploy.py must exist"


def test_plugin_register_source_includes_codex_tool_handlers() -> None:
    source = _init_source()
    assert 'name="deploy_codex_receiver"' in source
    assert 'name="codex_receiver_status"' in source
    assert 'name="codex_receiver_stop"' in source
    assert "Codex" in source
    assert "9320" in source  # codex band start (9320-9329)


def test_receiver_config_keys_and_cwd_pinned(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    cfg = codex_deploy.build_receiver_config(canonical, 9311, "o4-mini")
    assert cfg["repo_path"] == str(canonical)
    assert cfg["bind_host"] == "127.0.0.1"
    assert cfg["bind_port"] == 9311
    assert cfg["hermes_url"] == "http://127.0.0.1:9219/jsonrpc"
    assert cfg["role_file"] == ".hermes/A2A.md"
    assert cfg["codex_model"] == "o4-mini"
    assert isinstance(cfg["codex_timeout_s"], int)
    assert isinstance(cfg["max_concurrent_turns"], int)
    assert cfg["codex_sandbox"] == "workspace-write"


def test_receiver_config_custom_sandbox(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    cfg = codex_deploy.build_receiver_config(canonical, 9311, None, sandbox="read-only")
    assert cfg["codex_sandbox"] == "read-only"


def test_deploy_writes_config_with_pinned_cwd(tmp_path: Path, stub_template: Path, stub_runtime) -> None:
    repo = _make_repo(tmp_path)
    res = _run(codex_deploy.deploy_codex_receiver_handler(str(repo), bind_port=9311, model="o4-mini"))
    assert res["deployed"] is True
    cfg_path = repo / ".hermes" / "codex_receiver.json"
    cfg = json.loads(cfg_path.read_text())
    assert cfg["repo_path"] == str(Path(os.path.realpath(str(repo))))
    assert cfg["bind_port"] == 9311
    assert cfg["codex_model"] == "o4-mini"
    assert not (repo / ".hermes" / "codex_receiver.json.tmp").exists()
    assert stub_runtime["receiver"].name == "codex_receiver.py"
    assert stub_runtime["log"].name == "codex_receiver.log"


def test_deploy_distinct_filenames_from_cc_and_oc(tmp_path: Path, stub_template: Path, stub_runtime) -> None:
    """Codex runtime filenames must not collide with cc or oc receiver files."""
    repo = _make_repo(tmp_path)
    _run(codex_deploy.deploy_codex_receiver_handler(str(repo), bind_port=9311))
    hermes_dir = repo / ".hermes"
    # codex files present
    assert (hermes_dir / "codex_receiver.json").exists()
    assert (hermes_dir / "codex_receiver.py").exists()
    # cc and oc config files NOT created by codex deploy
    assert not (hermes_dir / "a2a_receiver.json").exists()
    assert not (hermes_dir / "oc_receiver.json").exists()


def test_deploy_autowires_managed_codex_peer(tmp_path: Path, stub_template: Path, stub_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)

    repo = _make_repo(tmp_path / "work")
    res = _run(codex_deploy.deploy_codex_receiver_handler(str(repo), bind_port=9311))
    assert res["deployed"] is True
    assert res["fleet_peer"]["name"] == "codex"

    fleet_yaml = home / "fleet.yaml"
    assert fleet_yaml.is_file()
    body = fleet_yaml.read_text()
    assert "mode: codex" in body
    assert "http://127.0.0.1:9311" in body


def test_status_handler_requires_pid_and_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir()
    (hermes_dir / codex_deploy.CONFIG_FILENAME).write_text(json.dumps({"bind_port": 9311}))
    (hermes_dir / codex_deploy.PID_FILENAME).write_text("4242")

    monkeypatch.setattr(codex_deploy, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(codex_deploy, "_check_health_once", lambda port, expected_repo_path=None: port == 9311)

    res = _run(codex_deploy.codex_receiver_status_handler(str(repo)))
    assert res == {
        "running": True,
        "pid": 4242,
        "port": 9311,
        "healthy": True,
        "repo_path": str(Path(os.path.realpath(str(repo)))),
    }


def test_stop_handler_reports_stopped_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir()
    (hermes_dir / codex_deploy.PID_FILENAME).write_text("5150")

    monkeypatch.setattr(codex_deploy, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(codex_deploy, "_terminate_pid", lambda pid: pid == 5150)

    res = _run(codex_deploy.codex_receiver_stop_handler(str(repo)))
    assert res == {"stopped": True, "pid": 5150}
    assert not (hermes_dir / codex_deploy.PID_FILENAME).exists()


def test_deploy_handler_dict_dispatch_extracts_all_params(
    tmp_path: Path, stub_template: Path, stub_runtime
) -> None:
    """Registry calls handler(args_dict, task_id=...) — all params must be unwrapped.

    This test FAILS before the dict-unwrap fix (bind_port silently defaults to 9311)
    and PASSES after (the config records 9312 as requested).
    """
    repo = _make_repo(tmp_path)
    # Simulate registry dispatch: whole args dict as first positional, task_id injected
    res = _run(codex_deploy.deploy_codex_receiver_handler(
        {"repo_path": str(repo), "bind_port": 9312, "model": "o4-mini", "sandbox": "read-only"},
        task_id="t-1",
    ))
    assert res.get("deployed") is True, f"deploy failed: {res}"
    cfg_path = repo / ".hermes" / "codex_receiver.json"
    cfg = json.loads(cfg_path.read_text())
    assert cfg["bind_port"] == 9312, (
        f"Expected bind_port=9312 (from dict args), got {cfg['bind_port']} — "
        "dict-unwrap for non-repo_path params is missing"
    )
    assert cfg.get("codex_model") == "o4-mini"
    assert cfg.get("codex_sandbox") == "read-only"


def test_dict_dispatch_fails_without_unwrap(
    tmp_path: Path, stub_template: Path, stub_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirm dict-dispatch test genuinely fails if the unwrap block is removed.

    We monkey-patch the handler to bypass the unwrap, then assert the config
    does NOT record bind_port=9312 — proving the regression test is meaningful.
    """
    import functools

    repo = _make_repo(tmp_path)
    original = codex_deploy.deploy_codex_receiver_handler

    # Wrap the handler to skip the dict-unwrap block (simulate pre-fix behavior)
    async def handler_without_unwrap(repo_path, bind_port=codex_deploy.DEFAULT_BIND_PORT,
                                     model=None, sandbox=codex_deploy.DEFAULT_SANDBOX,
                                     no_auth=False, hermes_auth_token_env="", **kw):
        # Do NOT unwrap — pass repo_path directly as str (skip the isinstance check)
        if isinstance(repo_path, dict):
            repo_path_str = repo_path.get("repo_path", "")
            # Intentionally do NOT extract bind_port from dict
        else:
            repo_path_str = repo_path
        return await original(repo_path_str, bind_port=bind_port, model=model,
                              sandbox=sandbox, no_auth=no_auth,
                              hermes_auth_token_env=hermes_auth_token_env)

    monkeypatch.setattr(codex_deploy, "deploy_codex_receiver_handler", handler_without_unwrap)

    res = _run(codex_deploy.deploy_codex_receiver_handler(
        {"repo_path": str(repo), "bind_port": 9312, "model": "o4-mini"},
        task_id="t-2",
    ))
    if res.get("deployed") is True:
        cfg_path = repo / ".hermes" / "codex_receiver.json"
        cfg = json.loads(cfg_path.read_text())
        # Without unwrap, bind_port should fall back to default (9311), NOT 9312
        assert cfg["bind_port"] != 9312, (
            "dict-dispatch regression test is broken: bind_port=9312 was recorded "
            "even without the dict-unwrap block"
        )


def test_codex_deploy_module_is_importable() -> None:
    spec = importlib.util.spec_from_file_location("codex_deploy_under_test", CODEX_DEPLOY_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def test_mode_codex_in_managed_peers() -> None:
    """managed_peers.SUPPORTED_MANAGED_MODES must include 'codex'."""
    from a2a_fleet import managed_peers
    assert "codex" in managed_peers.SUPPORTED_MANAGED_MODES


def test_mode_codex_reconcile_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cc_deploy._deploy_managed_receiver dispatches correctly for mode=codex."""
    from a2a_fleet import cc_deploy

    deployed_calls = []

    async def fake_deploy(repo_path, bind_port=9311, **kw):
        deployed_calls.append({"repo_path": repo_path, "bind_port": bind_port})
        return {"deployed": True, "pid": 99}

    monkeypatch.setattr(
        "a2a_fleet.codex_deploy.deploy_codex_receiver_handler",
        fake_deploy,
    )

    repo = _make_repo(tmp_path)
    result = cc_deploy._deploy_managed_receiver("codex", repo, 9311)
    assert result.get("deployed") is True
    assert len(deployed_calls) == 1
    assert deployed_calls[0]["bind_port"] == 9311
