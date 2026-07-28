"""Tests for the three read-only Herdr tool handlers in herdr_tools.py.

All tests mock herdr — no test may depend on a live herdr binary or a
running herdr server, since these run in CI. ``check_herdr_capability`` and
``HerdrClient`` methods are patched; the config layer (``fleet.herdr``) is
exercised for real via the ``fleet_home`` fixture's fleet.yaml, mirroring
test_herdr_config_layer.py.

Dispatch shape coverage mirrors test_fleet_tools.py: ``registry.dispatch()``
calls ``handler(args, **kwargs)`` — the WHOLE args dict lands in the first
positional slot and ``task_id`` is injected as a kwarg. That is the live
gateway path and a real past bug source (TypeError before the handler's own
logic ever runs), so it is asserted for all three handlers here.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

import a2a_fleet.herdr_tools as herdr_tools
from a2a_fleet.herdr_client import HerdrClient, HerdrError, HerdrNotFound


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# fleet.yaml helpers (mirrors test_herdr_config_layer.py's pattern)
# ---------------------------------------------------------------------------


def _fleet_yaml_path(fleet_home: Path) -> Path:
    return fleet_home / "profiles" / "switch" / "fleet.yaml"


def _load_data(fleet_home: Path) -> Dict[str, Any]:
    return yaml.safe_load(_fleet_yaml_path(fleet_home).read_text())


def _write_data(fleet_home: Path, data: Dict[str, Any]) -> None:
    _fleet_yaml_path(fleet_home).write_text(yaml.safe_dump(data))


def _add_herdr_host(
    fleet_home: Path,
    alias: str = "mac-mini",
    *,
    allowed_workspaces: list[str] | None = None,
) -> None:
    data = _load_data(fleet_home)
    herdr_block = data["fleet"].setdefault("herdr", {})
    hosts = herdr_block.setdefault("hosts", {})
    hosts[alias] = {
        "transport": "local_socket",
        "allowed_workspaces": allowed_workspaces or ["/srv/workspaces/project-a"],
    }
    _write_data(fleet_home, data)


# ---------------------------------------------------------------------------
# Capability-probe stubs
# ---------------------------------------------------------------------------


def _patch_capability_result(monkeypatch: pytest.MonkeyPatch, result: Dict[str, Any]) -> None:
    async def _fake(host_alias, herdr_cfg=None, **_kw):
        return dict(result)

    monkeypatch.setattr(herdr_tools, "check_herdr_capability", _fake)


def _patch_capability_ok(monkeypatch: pytest.MonkeyPatch, host_alias: str = "mac-mini") -> None:
    _patch_capability_result(
        monkeypatch,
        {
            "status": "ok",
            "host_alias": host_alias,
            "version": "0.7.4",
            "protocol": 16,
            "transport": "local_socket",
            "socket": "/tmp/herdr.sock",
        },
    )


def _patch_client_calls(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    """Spy on HerdrClient.list_agents/get_agent without changing default no-op behavior."""

    async def fake_list_agents(self):
        calls.append("list_agents")
        return {"agents": []}

    async def fake_get_agent(self, target):
        calls.append(("get_agent", target))
        return {"agent": {}}

    monkeypatch.setattr(HerdrClient, "list_agents", fake_list_agents)
    monkeypatch.setattr(HerdrClient, "get_agent", fake_get_agent)


_HANDLERS = {
    "status": herdr_tools.herdr_status_handler,
    "list_sessions": herdr_tools.herdr_list_sessions_handler,
    "inspect_session": herdr_tools.herdr_inspect_session_handler,
}


# ---------------------------------------------------------------------------
# Dispatch shape: dict-first-positional vs kwargs, task_id absorption
# ---------------------------------------------------------------------------


def test_status_dispatch_shape_dict_and_kwargs_identical(fleet_home: Path) -> None:
    via_dict = _run(herdr_tools.herdr_status_handler({"host_alias": "mac-mini"}, task_id="t-1"))
    via_kwargs = _run(herdr_tools.herdr_status_handler(host_alias="mac-mini", task_id="t-1"))

    assert via_dict == via_kwargs
    assert via_dict["status"] == "unknown_host_alias"
    assert via_dict["known_hosts"] == []


def test_list_sessions_dispatch_shape_dict_and_kwargs_identical(fleet_home: Path) -> None:
    via_dict = _run(herdr_tools.herdr_list_sessions_handler({"host_alias": "mac-mini"}, task_id="t-1"))
    via_kwargs = _run(herdr_tools.herdr_list_sessions_handler(host_alias="mac-mini", task_id="t-1"))

    assert via_dict == via_kwargs
    assert via_dict["status"] == "unknown_host_alias"


def test_inspect_session_dispatch_shape_dict_and_kwargs_identical(fleet_home: Path) -> None:
    via_dict = _run(
        herdr_tools.herdr_inspect_session_handler(
            {"host_alias": "mac-mini", "terminal_id": "term_1"}, task_id="t-1"
        )
    )
    via_kwargs = _run(
        herdr_tools.herdr_inspect_session_handler(
            host_alias="mac-mini", terminal_id="term_1", task_id="t-1"
        )
    )

    assert via_dict == via_kwargs
    assert via_dict["status"] == "unknown_host_alias"


# ---------------------------------------------------------------------------
# Preflight / config
# ---------------------------------------------------------------------------


def test_absent_herdr_block_returns_unknown_host_alias_with_empty_known_hosts(
    fleet_home: Path,
) -> None:
    # Base fleet_home fixture has no fleet.herdr block at all.
    for handler in _HANDLERS.values():
        result = _run(handler(host_alias="mac-mini", terminal_id="term_1"))
        assert result["status"] == "unknown_host_alias"
        assert result["known_hosts"] == []


def test_unknown_alias_with_hosts_configured_lists_known_hosts(fleet_home: Path) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini")

    for handler in _HANDLERS.values():
        result = _run(handler(host_alias="does-not-exist", terminal_id="term_1"))
        assert result["status"] == "unknown_host_alias"
        assert result["known_hosts"] == ["mac-mini"]


def test_capability_non_ok_returned_verbatim_no_client_call(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini")
    capability_result = {
        "status": "herdr_unreachable",
        "reason": "herdr server not running (status=None)",
        "host_alias": "mac-mini",
        "socket": "/tmp/herdr.sock",
    }
    _patch_capability_result(monkeypatch, capability_result)
    calls: list = []
    _patch_client_calls(monkeypatch, calls)

    for handler in _HANDLERS.values():
        result = _run(handler(host_alias="mac-mini", terminal_id="term_1"))
        assert result == capability_result

    assert calls == []


# ---------------------------------------------------------------------------
# herdr_list_sessions
# ---------------------------------------------------------------------------


def _agent_record(**overrides: Any) -> Dict[str, Any]:
    base = {
        "terminal_id": "term_1",
        "pane_id": "pane_1",
        "agent": "claude",
        "agent_status": "idle",
        "cwd": "/srv/workspaces/project-a/session1",
        "workspace_id": "ws-1",
        "revision": 1,
        "terminal_title_stripped": "claude",
        "focused": False,
    }
    base.update(overrides)
    return base


def test_list_sessions_happy_path_normalizes_and_counts(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini")
    _patch_capability_ok(monkeypatch)

    record = _agent_record()

    async def fake_list_agents(self):
        return {"agents": [record]}

    monkeypatch.setattr(HerdrClient, "list_agents", fake_list_agents)

    result = _run(herdr_tools.herdr_list_sessions_handler(host_alias="mac-mini"))

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["filtered_out_by_allowlist"] == 0
    assert result["sessions"] == [
        {
            "terminal_id": "term_1",
            "pane_id": "pane_1",
            "agent_kind": "claude",
            "agent_status": "idle",
            "cwd": "/srv/workspaces/project-a/session1",
            "workspace_id": "ws-1",
            "revision": 1,
            "terminal_title_stripped": "claude",
            "focused": False,
        }
    ]


def test_list_sessions_allowlist_enforcement_excludes_and_counts(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini", allowed_workspaces=["/srv/workspaces/project-a"])
    _patch_capability_ok(monkeypatch)

    inside = _agent_record(terminal_id="term_in", cwd="/srv/workspaces/project-a/session1")
    outside = _agent_record(terminal_id="term_out", cwd="/srv/workspaces/other-project")

    async def fake_list_agents(self):
        return {"agents": [inside, outside]}

    monkeypatch.setattr(HerdrClient, "list_agents", fake_list_agents)

    result = _run(herdr_tools.herdr_list_sessions_handler(host_alias="mac-mini"))

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["filtered_out_by_allowlist"] == 1
    assert [s["terminal_id"] for s in result["sessions"]] == ["term_in"]


def test_list_sessions_prefix_escape_guard_rejects_lookalike_dir(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security-relevant case: allowed_workspaces ["/srv/workspaces/project-a"] must
    NOT admit a session whose cwd is "/srv/workspaces/project-a-evil" — a naive
    string-prefix check would wrongly allow this."""
    _add_herdr_host(fleet_home, alias="mac-mini", allowed_workspaces=["/srv/workspaces/project-a"])
    _patch_capability_ok(monkeypatch)

    evil = _agent_record(terminal_id="term_evil", cwd="/srv/workspaces/project-a-evil")

    async def fake_list_agents(self):
        return {"agents": [evil]}

    monkeypatch.setattr(HerdrClient, "list_agents", fake_list_agents)

    result = _run(herdr_tools.herdr_list_sessions_handler(host_alias="mac-mini"))

    assert result["status"] == "ok"
    assert result["count"] == 0
    assert result["filtered_out_by_allowlist"] == 1
    assert result["sessions"] == []


def test_list_sessions_workspace_filter_narrows_results(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini", allowed_workspaces=["/srv/workspaces/project-a"])
    _patch_capability_ok(monkeypatch)

    a = _agent_record(terminal_id="term_a", cwd="/srv/workspaces/project-a/repo1")
    b = _agent_record(terminal_id="term_b", cwd="/srv/workspaces/project-a/repo2")

    async def fake_list_agents(self):
        return {"agents": [a, b]}

    monkeypatch.setattr(HerdrClient, "list_agents", fake_list_agents)

    result = _run(
        herdr_tools.herdr_list_sessions_handler(
            host_alias="mac-mini", workspace="/srv/workspaces/project-a/repo2"
        )
    )

    assert result["count"] == 1
    assert result["sessions"][0]["terminal_id"] == "term_b"


def test_list_sessions_agent_kind_filter_narrows_results(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini", allowed_workspaces=["/srv/workspaces/project-a"])
    _patch_capability_ok(monkeypatch)

    claude = _agent_record(terminal_id="term_claude", agent="claude", cwd="/srv/workspaces/project-a/r1")
    opencode = _agent_record(terminal_id="term_oc", agent="opencode", cwd="/srv/workspaces/project-a/r2")

    async def fake_list_agents(self):
        return {"agents": [claude, opencode]}

    monkeypatch.setattr(HerdrClient, "list_agents", fake_list_agents)

    result = _run(
        herdr_tools.herdr_list_sessions_handler(host_alias="mac-mini", agent_kind="opencode")
    )

    assert result["count"] == 1
    assert result["sessions"][0]["terminal_id"] == "term_oc"


def test_list_sessions_herdr_error_returns_error_status_not_raise(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini")
    _patch_capability_ok(monkeypatch)

    async def fake_list_agents(self):
        raise HerdrError("socket closed mid-call", code="unknown")

    monkeypatch.setattr(HerdrClient, "list_agents", fake_list_agents)

    result = _run(herdr_tools.herdr_list_sessions_handler(host_alias="mac-mini"))

    assert result["status"] == "herdr_error"
    assert "socket closed mid-call" in result["reason"]


# ---------------------------------------------------------------------------
# herdr_inspect_session
# ---------------------------------------------------------------------------


def test_inspect_session_missing_terminal_id_returns_error_no_client_call(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini")
    _patch_capability_ok(monkeypatch)
    calls: list = []
    _patch_client_calls(monkeypatch, calls)

    result = _run(herdr_tools.herdr_inspect_session_handler(host_alias="mac-mini", terminal_id=""))

    assert "error" in result
    assert calls == []


def test_inspect_session_absent_terminal_id_returns_error_no_client_call(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini")
    _patch_capability_ok(monkeypatch)
    calls: list = []
    _patch_client_calls(monkeypatch, calls)

    result = _run(herdr_tools.herdr_inspect_session_handler(host_alias="mac-mini"))

    assert "error" in result
    assert calls == []


def test_inspect_session_not_found(fleet_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini")
    _patch_capability_ok(monkeypatch)

    async def fake_get_agent(self, target):
        raise HerdrNotFound(f"no such target: {target}", code="agent_not_found")

    monkeypatch.setattr(HerdrClient, "get_agent", fake_get_agent)

    result = _run(
        herdr_tools.herdr_inspect_session_handler(host_alias="mac-mini", terminal_id="term_ghost")
    )

    assert result["status"] == "not_found"


def test_inspect_session_outside_allowlist_returns_workspace_denied(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini", allowed_workspaces=["/srv/workspaces/project-a"])
    _patch_capability_ok(monkeypatch)

    record = _agent_record(cwd="/srv/workspaces/other-project")

    async def fake_get_agent(self, target):
        return {"agent": record}

    monkeypatch.setattr(HerdrClient, "get_agent", fake_get_agent)

    result = _run(
        herdr_tools.herdr_inspect_session_handler(host_alias="mac-mini", terminal_id="term_1")
    )

    assert result["status"] == "workspace_denied"


def test_inspect_session_happy_path_returns_normalized_session(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini", allowed_workspaces=["/srv/workspaces/project-a"])
    _patch_capability_ok(monkeypatch)

    record = _agent_record(cwd="/srv/workspaces/project-a/repo1")

    async def fake_get_agent(self, target):
        assert target == "term_1"
        return {"agent": record}

    monkeypatch.setattr(HerdrClient, "get_agent", fake_get_agent)

    result = _run(
        herdr_tools.herdr_inspect_session_handler(host_alias="mac-mini", terminal_id="term_1")
    )

    assert result["status"] == "ok"
    assert result["session"]["terminal_id"] == "term_1"
    assert result["session"]["cwd"] == "/srv/workspaces/project-a/repo1"


# ---------------------------------------------------------------------------
# Robustness: unexpected exceptions never propagate
# ---------------------------------------------------------------------------


def test_status_handler_unexpected_exception_returns_error_dict(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini")

    async def _boom(host_alias, herdr_cfg=None, **_kw):
        raise RuntimeError("capability probe exploded")

    monkeypatch.setattr(herdr_tools, "check_herdr_capability", _boom)

    result = _run(herdr_tools.herdr_status_handler(host_alias="mac-mini"))

    assert "error" in result


def test_list_sessions_unexpected_exception_returns_error_dict(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini")
    _patch_capability_ok(monkeypatch)

    async def fake_list_agents(self):
        raise RuntimeError("totally unrelated crash")

    monkeypatch.setattr(HerdrClient, "list_agents", fake_list_agents)

    result = _run(herdr_tools.herdr_list_sessions_handler(host_alias="mac-mini"))

    assert "error" in result


def test_inspect_session_unexpected_exception_returns_error_dict(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_herdr_host(fleet_home, alias="mac-mini")
    _patch_capability_ok(monkeypatch)

    async def fake_get_agent(self, target):
        raise RuntimeError("totally unrelated crash")

    monkeypatch.setattr(HerdrClient, "get_agent", fake_get_agent)

    result = _run(
        herdr_tools.herdr_inspect_session_handler(host_alias="mac-mini", terminal_id="term_1")
    )

    assert "error" in result
