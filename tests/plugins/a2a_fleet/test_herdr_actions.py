"""Phase 2 acceptance tests: confirmation-gated Herdr actions and takeover.

These map one-to-one onto the plan's Phase 2 acceptance criteria:

- an attempted stale or reused confirmation is rejected;
- a human takeover blocks automation without terminating the Herdr session;
- completed / failed / unknown-outcome paths each leave a truthful record;
- no result is reported as complete from silence alone.

The store is redirected to a temp SQLite file; the Herdr CLI is never invoked.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

import a2a_fleet.herdr_binding as herdr_binding
import a2a_fleet.herdr_tools as herdr_tools
from a2a_fleet.herdr_client import HerdrClient

SESSION = {
    "agent": "claude",
    "agent_status": "idle",
    "cwd": "/srv/workspaces/project-a",
    "pane_id": "w1:pH",
    "terminal_id": "term_abc123",
    "workspace_id": "w1",
    "revision": 40,
}


def _run(coro):
    return asyncio.run(coro)


def _fleet_yaml_path(fleet_home: Path) -> Path:
    return fleet_home / "profiles" / "switch" / "fleet.yaml"


def _add_host(fleet_home: Path, *, allow_actions: bool = True) -> None:
    data = yaml.safe_load(_fleet_yaml_path(fleet_home).read_text())
    hosts = data["fleet"].setdefault("herdr", {}).setdefault("hosts", {})
    hosts["mac-mini"] = {
        "transport": "local_socket",
        "allowed_workspaces": ["/srv/workspaces/project-a"],
        "allow_actions": allow_actions,
    }
    _fleet_yaml_path(fleet_home).write_text(yaml.safe_dump(data))


@pytest.fixture
def herdr_env(fleet_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Configured host, stubbed capability probe, temp binding store."""
    _add_host(fleet_home)

    async def _capability_ok(host_alias, herdr_cfg=None, **_kw):
        return {"status": "ok", "host_alias": host_alias, "protocol": 16}

    monkeypatch.setattr(herdr_tools, "check_herdr_capability", _capability_ok)
    monkeypatch.setattr(herdr_binding, "db_path", lambda: tmp_path / "state.db")

    live: Dict[str, Any] = {"session": dict(SESSION)}

    async def fake_get_agent(self, target):
        return {"agent": dict(live["session"])}

    monkeypatch.setattr(HerdrClient, "get_agent", fake_get_agent)
    return live


def _audit(tmp_path: Path) -> List[Dict[str, Any]]:
    conn = herdr_binding.connect(tmp_path / "state.db")
    try:
        return herdr_binding.recent_audit(conn, "mac-mini", "term_abc123", limit=50)
    finally:
        conn.close()


def _preview(action: str = "run the tests") -> Dict[str, Any]:
    return _run(
        herdr_tools.herdr_preview_action_handler(
            host_alias="mac-mini", terminal_id="term_abc123", action=action,
            summary="phase 2 check",
        )
    )


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def test_preview_mints_token_and_mutates_nothing(herdr_env, monkeypatch) -> None:
    sent: List[Any] = []

    async def fake_send(self, target, text):
        sent.append((target, text))
        return {}

    monkeypatch.setattr(HerdrClient, "send_agent_text", fake_send)

    result = _preview()
    assert result["status"] == "preview"
    assert result["confirmation_token"]
    assert result["revision_at_preview"] == 40
    assert result["completion_signal"] == herdr_tools.COMPLETION_SIGNAL
    assert sent == [], "preview must not send anything"


def test_actions_refused_until_host_opts_in(fleet_home, tmp_path, monkeypatch) -> None:
    """read_only_default means a host configured for inspection cannot write."""
    _add_host(fleet_home, allow_actions=False)

    async def _capability_ok(host_alias, herdr_cfg=None, **_kw):
        return {"status": "ok", "host_alias": host_alias, "protocol": 16}

    monkeypatch.setattr(herdr_tools, "check_herdr_capability", _capability_ok)
    monkeypatch.setattr(herdr_binding, "db_path", lambda: tmp_path / "state.db")

    async def fake_get_agent(self, target):
        return {"agent": dict(SESSION)}

    monkeypatch.setattr(HerdrClient, "get_agent", fake_get_agent)

    result = _preview()
    assert result["status"] == "actions_disabled"
    assert "allow_actions" in result["reason"]


# ---------------------------------------------------------------------------
# Request: token discipline
# ---------------------------------------------------------------------------


def _request(token: str, **kw) -> Dict[str, Any]:
    return _run(
        herdr_tools.herdr_request_action_handler(
            host_alias="mac-mini", terminal_id="term_abc123",
            confirmation_token=token, **kw,
        )
    )


def test_confirmed_action_sends_once_and_records_it(herdr_env, tmp_path, monkeypatch) -> None:
    sent: List[Any] = []

    async def fake_send(self, target, text):
        sent.append((target, text))
        return {}

    monkeypatch.setattr(HerdrClient, "send_agent_text", fake_send)

    preview = _preview("run the tests")
    result = _request(preview["confirmation_token"])

    assert result["status"] == "sent"
    assert sent == [("term_abc123", "run the tests")]
    # Not complete: nothing waited for a done signal.
    assert result["completion_state"] == "pending"
    events = [row["event"] for row in _audit(tmp_path)]
    assert "send_attempted" in events and "sent" in events


def test_token_is_single_use(herdr_env, monkeypatch) -> None:
    calls: List[Any] = []

    async def fake_send(self, target, text):
        calls.append(text)
        return {}

    monkeypatch.setattr(HerdrClient, "send_agent_text", fake_send)

    preview = _preview()
    assert _request(preview["confirmation_token"])["status"] == "sent"
    replay = _request(preview["confirmation_token"])
    assert replay["status"] == "token_already_used"
    assert len(calls) == 1, "a replayed token must not produce a second send"


def test_unknown_token_rejected(herdr_env) -> None:
    assert _request("not-a-real-token")["status"] == "unknown_token"


def test_expired_token_rejected(herdr_env, monkeypatch) -> None:
    monkeypatch.setattr(herdr_binding, "TOKEN_TTL_SECONDS", -1)
    preview = _preview()
    assert _request(preview["confirmation_token"])["status"] == "token_expired"


def test_stale_revision_refuses_send(herdr_env, tmp_path, monkeypatch) -> None:
    """The pane moved between preview and confirmation: refuse, don't guess."""
    sent: List[Any] = []

    async def fake_send(self, target, text):
        sent.append(text)
        return {}

    monkeypatch.setattr(HerdrClient, "send_agent_text", fake_send)

    preview = _preview()
    herdr_env["session"]["revision"] = 41  # output advanced under us

    result = _request(preview["confirmation_token"])
    assert result["status"] == "revision_moved"
    assert result["revision_at_preview"] == 40 and result["revision_now"] == 41
    assert sent == []
    assert "rejected_stale" in [row["event"] for row in _audit(tmp_path)]


def test_token_bound_to_its_own_session(herdr_env, monkeypatch) -> None:
    """A token minted for one session cannot be spent on a different one.

    Both sessions are real and resolvable, so this exercises the token's
    session binding rather than incidentally tripping the not-found path.
    """
    second = dict(SESSION, terminal_id="term_second", pane_id="w1:pJ")
    sent: List[Any] = []

    async def fake_get_agent(self, target):
        if target == "term_second":
            return {"agent": second}
        return {"agent": dict(herdr_env["session"])}

    async def fake_send(self, target, text):
        sent.append(target)
        return {}

    monkeypatch.setattr(HerdrClient, "get_agent", fake_get_agent)
    monkeypatch.setattr(HerdrClient, "send_agent_text", fake_send)

    preview = _preview()  # token is for term_abc123
    other = _run(
        herdr_tools.herdr_request_action_handler(
            host_alias="mac-mini", terminal_id="term_second",
            confirmation_token=preview["confirmation_token"],
        )
    )
    assert other["status"] == "token_session_mismatch"
    assert sent == []
    # The token survives the misdirected attempt — it was never claimed.
    assert _request(preview["confirmation_token"])["status"] == "sent"


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def test_send_failure_is_unknown_not_failed_silently(herdr_env, tmp_path, monkeypatch) -> None:
    """A dropped connection leaves a truthful record and forbids retry."""

    async def boom(self, target, text):
        raise OSError("connection reset")

    monkeypatch.setattr(HerdrClient, "send_agent_text", boom)

    preview = _preview()
    result = _request(preview["confirmation_token"])

    assert result["status"] == "outcome_unknown"
    assert "never" in result["retry"]
    assert "send_outcome_unknown" in [row["event"] for row in _audit(tmp_path)]


def test_wait_timeout_is_not_completion(herdr_env, tmp_path, monkeypatch) -> None:
    """Silence is never completion evidence."""

    async def fake_send(self, target, text):
        return {}

    async def timeout_wait(self, pane_id, status, timeout_ms):
        raise TimeoutError("no done signal")

    monkeypatch.setattr(HerdrClient, "send_agent_text", fake_send)
    monkeypatch.setattr(HerdrClient, "wait_agent_status", timeout_wait)

    preview = _preview()
    result = _request(preview["confirmation_token"], wait_timeout_ms=50)

    assert result["status"] == "sent"
    assert result["completion_state"] == "pending"
    assert "not treated as complete" in result["completion_note"].lower()


def test_done_signal_marks_completed(herdr_env, tmp_path, monkeypatch) -> None:
    async def fake_send(self, target, text):
        return {}

    async def done_wait(self, pane_id, status, timeout_ms):
        assert status == "done", "completion must be the done signal, not idle"
        return {}

    monkeypatch.setattr(HerdrClient, "send_agent_text", fake_send)
    monkeypatch.setattr(HerdrClient, "wait_agent_status", done_wait)

    preview = _preview()
    result = _request(preview["confirmation_token"], wait_timeout_ms=50)
    assert result["completion_state"] == "completed"

    conn = herdr_binding.connect(tmp_path / "state.db")
    try:
        binding = herdr_binding.get_binding(conn, "mac-mini", "term_abc123")
    finally:
        conn.close()
    assert binding["completion_state"] == "completed"


# ---------------------------------------------------------------------------
# Human takeover
# ---------------------------------------------------------------------------


def test_takeover_blocks_automation_without_touching_the_session(
    herdr_env, monkeypatch
) -> None:
    sent: List[Any] = []
    herdr_calls: List[Any] = []

    async def fake_send(self, target, text):
        sent.append(text)
        return {}

    async def spy_call(self, *args, **kw):
        herdr_calls.append(args)
        return {}

    monkeypatch.setattr(HerdrClient, "send_agent_text", fake_send)
    monkeypatch.setattr(HerdrClient, "_call", spy_call)

    preview = _preview()
    claim = _run(
        herdr_tools.herdr_claim_human_takeover_handler(
            host_alias="mac-mini", terminal_id="term_abc123"
        )
    )
    assert claim["status"] == "human_takeover"

    blocked = _request(preview["confirmation_token"])
    assert blocked["status"] == "human_takeover"
    assert sent == []
    # Fleet-side only: no Herdr authority verb was invoked on the pane.
    assert herdr_calls == []

    released = _run(
        herdr_tools.herdr_release_human_takeover_handler(
            host_alias="mac-mini", terminal_id="term_abc123"
        )
    )
    assert released["status"] == "automation_permitted"
    assert _request(preview["confirmation_token"])["status"] == "sent"


def test_takeover_survives_a_fresh_store_connection(herdr_env, tmp_path) -> None:
    """The pause is durable: it is SQLite, not the in-memory ContextStore.

    A gateway restart must not silently re-enable automation on a session a
    human has taken over — the Herdr session outlives the gateway.
    """
    _run(
        herdr_tools.herdr_claim_human_takeover_handler(
            host_alias="mac-mini", terminal_id="term_abc123"
        )
    )
    conn = herdr_binding.connect(tmp_path / "state.db")  # brand-new connection
    try:
        assert herdr_binding.automation_blocked(conn, "mac-mini", "term_abc123") is True
    finally:
        conn.close()
