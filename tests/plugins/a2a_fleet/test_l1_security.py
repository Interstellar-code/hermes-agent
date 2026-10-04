"""L1-01 / L1-03 / L1-04 regressions: /jsonrpc CSRF + DNS-rebinding guards,
non-loopback fail-closed start, reply URL follows bind_port, auth-on default
reply token, boot-reconcile preserves pinned receiver fields."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi", reason="a2a_fleet server tests require hermes-agent[web]")
from fastapi.testclient import TestClient  # noqa: E402

from a2a_fleet import cc_deploy  # noqa: E402

BODY = {
    "jsonrpc": "2.0",
    "id": "1",
    "method": "SendMessage",
    "params": {"message": {"role": "user", "parts": [{"text": "ping"}]}},
}


def _set_server(fleet_home: Path, **server) -> None:
    path = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(path.read_text())
    data["fleet"]["server"].update(server)
    path.write_text(yaml.safe_dump(data))


def test_jsonrpc_csrf_and_rebinding_guards(fleet_home: Path) -> None:
    from a2a_fleet.server import build_app

    with TestClient(build_app(), base_url="http://127.0.0.1:9319") as c:
        assert c.post("/jsonrpc", json=BODY).status_code == 200
        # text/plain "simple request" (no CORS preflight) -> 415
        r = c.post("/jsonrpc", content=json.dumps(BODY), headers={"content-type": "text/plain"})
        assert r.status_code == 415
        # any browser Origin -> 403
        assert c.post("/jsonrpc", json=BODY, headers={"origin": "http://evil.example"}).status_code == 403
        # DNS-rebinding Host -> 403 on every route
        bad = {"host": "attacker.example:9319"}
        assert c.post("/jsonrpc", json=BODY, headers=bad).status_code == 403
        assert c.get("/health", headers=bad).status_code == 403
        assert c.get("/.well-known/agent-card.json", headers=bad).status_code == 403
        for ok_host in ("localhost:9319", "[::1]:9319"):
            assert c.post("/jsonrpc", json=BODY, headers={"host": ok_host}).status_code == 200


def test_start_refuses_non_loopback_bind_without_token(fleet_home: Path) -> None:
    from a2a_fleet import server

    _set_server(fleet_home, bind_host="0.0.0.0", auth_required=False)
    with pytest.raises(server.A2AServerStartError, match="non-loopback"):
        asyncio.run(server.start_server())
    assert not server.is_running()


def test_reply_target_follows_bind_port_and_auth(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import hermes_reply_target

    assert hermes_reply_target() == ("http://127.0.0.1:9319", "")
    _set_server(fleet_home, auth_required=True)
    assert hermes_reply_target() == ("http://127.0.0.1:9319", "SWITCH_A2A_TOKEN")


def test_deploy_reply_url_and_default_token_env(fleet_home: Path, tmp_path: Path, monkeypatch) -> None:
    _set_server(fleet_home, auth_required=True)
    monkeypatch.setattr(cc_deploy, "_launch_receiver", lambda *a, **k: 4242)
    monkeypatch.setattr(cc_deploy, "_poll_health", lambda *a, **k: True)
    monkeypatch.setattr(cc_deploy, "_probe_claude_cli", lambda: True)
    monkeypatch.setattr(cc_deploy, "_stop_old_receiver", lambda pid_path: (None, None))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    res = asyncio.run(cc_deploy.deploy_cc_receiver_handler(str(repo), bind_port=9301))
    assert res.get("deployed") is True, res
    cfg = json.loads((repo / ".hermes" / cc_deploy.CONFIG_FILENAME).read_text())
    assert cfg["hermes_url"] == "http://127.0.0.1:9319/jsonrpc"
    assert cfg["hermes_auth_token_env"] == "SWITCH_A2A_TOKEN"
    assert "http://127.0.0.1:9319" in (repo / ".hermes" / cc_deploy.ROLE_FILENAME).read_text()


@pytest.mark.parametrize(
    "mode,cfg_name,pinned,expected",
    [
        ("claude_code", "a2a_receiver.json", {"claude_model": "opus"}, {"model": "opus"}),
        ("codex", "codex_receiver.json", {"codex_model": "o3", "codex_sandbox": "read-only"},
         {"model": "o3", "sandbox": "read-only"}),
        ("agy", "agy_receiver.json", {"agy_sandbox": False}, {"sandbox": False}),
    ],
)
def test_reconcile_redeploy_preserves_pinned_fields(tmp_path, monkeypatch, mode, cfg_name, pinned, expected):
    calls = []

    async def fake(repo_path, **kw):
        calls.append(kw)
        return {"deployed": True}

    module = cc_deploy._managed_receiver_module(mode)
    handler = cc_deploy._MANAGED_DEPLOY_SPEC[mode][0]
    monkeypatch.setattr(module, handler, fake)
    assert module.CONFIG_FILENAME == cfg_name
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / cfg_name).write_text(
        json.dumps({"bind_port": 1, "hermes_auth_token_env": "SWITCH_A2A_TOKEN", **pinned})
    )
    cc_deploy._deploy_managed_receiver(mode, tmp_path, 9301)
    assert calls == [{"bind_port": 9301, "hermes_auth_token_env": "SWITCH_A2A_TOKEN", **expected}]
