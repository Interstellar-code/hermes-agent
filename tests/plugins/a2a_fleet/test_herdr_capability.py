"""check_herdr_capability: structured, never-raising status matrix."""
from __future__ import annotations

import asyncio

import pytest


def _patch_which(monkeypatch: pytest.MonkeyPatch, found: bool) -> None:
    monkeypatch.setattr(
        "a2a_fleet.herdr_capability.shutil.which",
        lambda _name: "/usr/local/bin/herdr" if found else None,
    )


def _patch_client_methods(monkeypatch: pytest.MonkeyPatch, *, status=None, schema=None, help_text=None):
    from a2a_fleet.herdr_client import HerdrClient

    if status is not None:
        monkeypatch.setattr(HerdrClient, "status", status)
    if schema is not None:
        monkeypatch.setattr(HerdrClient, "schema", schema)
    if help_text is not None:
        monkeypatch.setattr(HerdrClient, "help_text", help_text)


def _ok_status(self):
    return {
        "client": {"version": "0.7.4"},
        "server": {"status": "running", "socket": "/tmp/herdr.sock"},
    }


def _ok_schema(self):
    return {"protocol": 16, "schema_version": 1}


def _ok_help(self, *_args):
    return "herdr agent list\nherdr agent get <target>\nherdr agent read <target>\nherdr wait agent-status <pane_id>"


async def _async(fn, *args):
    return fn(*args)


def test_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.herdr_capability import check_herdr_capability

    _patch_which(monkeypatch, found=False)

    result = asyncio.run(check_herdr_capability("mac-mini", {}))
    assert result["status"] == "herdr_missing"
    assert "reason" in result
    assert "install_hint" in result


def test_unreachable_when_status_call_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.herdr_capability import check_herdr_capability
    from a2a_fleet.herdr_client import HerdrUnavailable

    _patch_which(monkeypatch, found=True)

    async def raising_status(self):
        raise HerdrUnavailable("herdr exited 1: no server running")

    _patch_client_methods(monkeypatch, status=raising_status)

    result = asyncio.run(check_herdr_capability("mac-mini", {}))
    assert result["status"] == "herdr_unreachable"
    assert "no server running" in result["reason"]
    assert "socket" in result


def test_unreachable_when_server_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.herdr_capability import check_herdr_capability

    _patch_which(monkeypatch, found=True)

    async def stopped_status(self):
        return {"client": {"version": "0.7.4"}, "server": {"status": "stopped"}}

    _patch_client_methods(monkeypatch, status=stopped_status)

    result = asyncio.run(check_herdr_capability("mac-mini", {}))
    assert result["status"] == "herdr_unreachable"
    assert "stopped" in result["reason"]


def test_protocol_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.herdr_capability import check_herdr_capability

    _patch_which(monkeypatch, found=True)

    async def status(self):
        return _ok_status(self)

    async def mismatched_schema(self):
        return {"protocol": 15, "schema_version": 1}

    _patch_client_methods(monkeypatch, status=status, schema=mismatched_schema)

    result = asyncio.run(check_herdr_capability("mac-mini", {}))
    assert result["status"] == "herdr_protocol_mismatch"
    assert result["expected"] == 16
    assert result["actual"] == 15


def test_verbs_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.herdr_capability import check_herdr_capability

    _patch_which(monkeypatch, found=True)

    async def status(self):
        return _ok_status(self)

    async def schema(self):
        return _ok_schema(self)

    async def sparse_help(self, *_args):
        return "herdr agent list\n"  # missing get/read/wait agent-status

    _patch_client_methods(monkeypatch, status=status, schema=schema, help_text=sparse_help)

    result = asyncio.run(check_herdr_capability("mac-mini", {}))
    assert result["status"] == "herdr_verbs_missing"
    assert "agent get" in result["missing"]
    assert "wait agent-status" in result["missing"]


def test_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.herdr_capability import check_herdr_capability

    _patch_which(monkeypatch, found=True)

    async def status(self):
        return _ok_status(self)

    async def schema(self):
        return _ok_schema(self)

    async def help_text(self, *_args):
        return _ok_help(self, *_args)

    _patch_client_methods(monkeypatch, status=status, schema=schema, help_text=help_text)

    result = asyncio.run(check_herdr_capability("mac-mini", {}))
    assert result["status"] == "ok"
    assert result["protocol"] == 16
    assert result["version"] == "0.7.4"
    assert result["transport"] == "local_socket"
    assert result["host_alias"] == "mac-mini"


def test_ssh_bridge_transport_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.herdr_capability import check_herdr_capability

    _patch_which(monkeypatch, found=True)

    async def status(self):
        return _ok_status(self)

    async def schema(self):
        return _ok_schema(self)

    async def help_text(self, *_args):
        return _ok_help(self, *_args)

    _patch_client_methods(monkeypatch, status=status, schema=schema, help_text=help_text)

    herdr_cfg = {"hosts": {"build-host": {"transport": "ssh_bridge", "ssh_target": "build-host"}}}
    result = asyncio.run(check_herdr_capability("build-host", herdr_cfg))
    assert result["status"] == "ok"
    assert result["transport"] == "ssh_bridge"


def test_never_raises_on_unexpected_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.herdr_capability import check_herdr_capability

    _patch_which(monkeypatch, found=True)

    async def exploding_status(self):
        raise RuntimeError("something totally unrelated broke")

    _patch_client_methods(monkeypatch, status=exploding_status)

    result = asyncio.run(check_herdr_capability("mac-mini", {}))
    assert result["status"] == "herdr_unreachable"
