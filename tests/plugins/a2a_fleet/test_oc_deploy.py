"""Unit tests for a2a_fleet.oc_deploy (deploy_oc_receiver + companions).

These target the OpenCode contract in ``.omx/CONTRACTS.md``. Registration-parity
assertions may stay red until the plugin wiring lands.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest

from a2a_fleet import oc_deploy

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "a2a_fleet"
INIT_PATH = PLUGIN_DIR / "__init__.py"
OC_DEPLOY_PATH = PLUGIN_DIR / "oc_deploy.py"


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
    template = fake_pkg_dir / "templates" / oc_deploy.RECEIVER_FILENAME
    template.write_text("# fake receiver template\nprint('hi')\n")
    monkeypatch.setattr(oc_deploy, "__file__", str(fake_pkg_dir / "oc_deploy.py"))
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
        oc_deploy,
        "_launch_receiver",
        lambda repo, receiver_path, log_path, env=None: fake_launch(
            repo, receiver_path, log_path, env=env
        ),
    )
    monkeypatch.setattr(oc_deploy, "_poll_health", lambda port, budget_s=8.0, expected_repo_path=None: True)
    monkeypatch.setattr(oc_deploy, "_probe_opencode_cli", lambda: True)
    monkeypatch.setattr(oc_deploy, "_stop_old_receiver", lambda pid_path: (None, None))
    return launched


def test_oc_deploy_module_exists() -> None:
    assert OC_DEPLOY_PATH.is_file(), "plugins/a2a_fleet/oc_deploy.py must exist"


def test_plugin_register_source_includes_oc_tool_handlers() -> None:
    source = _init_source()
    assert 'name="deploy_oc_receiver"' in source
    assert 'name="oc_receiver_status"' in source
    assert 'name="oc_receiver_stop"' in source
    assert "OpenCode" in source
    assert "9310" in source


def test_receiver_config_keys_and_cwd_pinned(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    canonical = Path(os.path.realpath(str(repo)))
    cfg = oc_deploy.build_receiver_config(canonical, 9310, "gpt-oss")
    assert cfg["repo_path"] == str(canonical)
    assert cfg["bind_host"] == "127.0.0.1"
    assert cfg["bind_port"] == 9310
    assert cfg["hermes_url"] == "http://127.0.0.1:9219/jsonrpc"
    assert cfg["role_file"] == ".hermes/A2A.md"
    assert cfg["opencode_model"] == "gpt-oss"
    assert isinstance(cfg["opencode_timeout_s"], int)
    assert isinstance(cfg["max_concurrent_turns"], int)


def test_deploy_writes_config_with_pinned_cwd(tmp_path: Path, stub_template: Path, stub_runtime) -> None:
    repo = _make_repo(tmp_path)
    res = _run(oc_deploy.deploy_oc_receiver_handler(str(repo), bind_port=9311, model="gpt-oss"))
    assert res["deployed"] is True
    cfg_path = repo / ".hermes" / "oc_receiver.json"
    cfg = json.loads(cfg_path.read_text())
    assert cfg["repo_path"] == str(Path(os.path.realpath(str(repo))))
    assert cfg["bind_port"] == 9311
    assert cfg["opencode_model"] == "gpt-oss"
    assert not (repo / ".hermes" / "oc_receiver.json.tmp").exists()
    assert stub_runtime["receiver"].name == "oc_receiver.py"
    assert stub_runtime["log"].name == "oc_receiver.log"


def test_deploy_autowires_managed_opencode_peer(tmp_path: Path, stub_template: Path, stub_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)

    repo = _make_repo(tmp_path / "work")
    res = _run(oc_deploy.deploy_oc_receiver_handler(str(repo), bind_port=9310))
    assert res["deployed"] is True
    assert res["fleet_peer"]["name"] == "opencode"

    fleet_yaml = home / "fleet.yaml"
    assert fleet_yaml.is_file()
    body = fleet_yaml.read_text()
    assert "mode: opencode" in body
    assert "http://127.0.0.1:9310" in body


def test_status_handler_requires_pid_and_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir()
    (hermes_dir / oc_deploy.CONFIG_FILENAME).write_text(json.dumps({"bind_port": 9310}))
    (hermes_dir / oc_deploy.PID_FILENAME).write_text("4242")

    monkeypatch.setattr(oc_deploy, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(oc_deploy, "_check_health_once", lambda port, expected_repo_path=None: port == 9310)

    res = _run(oc_deploy.oc_receiver_status_handler(str(repo)))
    assert res == {
        "running": True,
        "pid": 4242,
        "port": 9310,
        "healthy": True,
        "repo_path": str(Path(os.path.realpath(str(repo)))),
    }


def test_stop_handler_reports_stopped_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir()
    (hermes_dir / oc_deploy.PID_FILENAME).write_text("5150")

    monkeypatch.setattr(oc_deploy, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(oc_deploy, "_terminate_pid", lambda pid: pid == 5150)

    res = _run(oc_deploy.oc_receiver_stop_handler(str(repo)))
    assert res == {"stopped": True, "pid": 5150}
    assert not (hermes_dir / oc_deploy.PID_FILENAME).exists()


def test_deploy_handler_dict_dispatch_extracts_all_params(
    tmp_path: Path, stub_template: Path, stub_runtime
) -> None:
    """Registry calls handler(args_dict, task_id=...) — all params must be unwrapped.

    This test FAILS before the dict-unwrap fix (bind_port silently defaults to 9310)
    and PASSES after (the config records 9311 as requested).
    """
    repo = _make_repo(tmp_path)
    # Simulate registry dispatch: whole args dict as first positional, task_id injected
    res = _run(oc_deploy.deploy_oc_receiver_handler(
        {"repo_path": str(repo), "bind_port": 9311, "model": "gpt-oss"},
        task_id="t-1",
    ))
    assert res.get("deployed") is True, f"deploy failed: {res}"
    cfg_path = repo / ".hermes" / "oc_receiver.json"
    cfg = json.loads(cfg_path.read_text())
    assert cfg["bind_port"] == 9311, (
        f"Expected bind_port=9311 (from dict args), got {cfg['bind_port']} — "
        "dict-unwrap for non-repo_path params is missing"
    )
    assert cfg.get("opencode_model") == "gpt-oss"


def test_oc_deploy_module_is_importable() -> None:
    spec = importlib.util.spec_from_file_location("oc_deploy_under_test", OC_DEPLOY_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
