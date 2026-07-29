"""Phase 3 acceptance tests: fleet_send routing to a herdr_session peer.

Maps onto the plan's Phase 3 criteria: a herdr peer is reachable through the
normal client path, an ambiguous session binding is a failure rather than a
guess, context_id continuity holds, and existing HTTP peer behaviour is
untouched.

Per the plan, Phase 3 must not modify any existing receiver/deploy test file —
so this is a new file and touches none of them.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

import a2a_fleet.herdr_binding as herdr_binding
import a2a_fleet.herdr_receiver as herdr_receiver
import a2a_fleet.herdr_tools as herdr_tools
from a2a_fleet.client import register_portless_handler
from a2a_fleet.herdr_client import HerdrClient

WORKSPACE = "/srv/workspaces/project-a"

SESSION_A = {
    "agent": "claude",
    "agent_status": "idle",
    "cwd": WORKSPACE,
    "pane_id": "w1:pA",
    "terminal_id": "term_aaa",
    "workspace_id": "w1",
    "revision": 5,
}
SESSION_B = dict(SESSION_A, terminal_id="term_bbb", pane_id="w1:pB")


def _run(coro):
    return asyncio.run(coro)


def _fleet_yaml(fleet_home: Path) -> Path:
    return fleet_home / "profiles" / "switch" / "fleet.yaml"


def _configure(
    fleet_home: Path, *, require_confirmation: bool = True, allow_actions: bool = True
) -> None:
    data = yaml.safe_load(_fleet_yaml(fleet_home).read_text())
    fleet = data["fleet"]
    fleet["herdr"] = {
        "require_confirmation_for_mutations": require_confirmation,
        "hosts": {
            "mac-mini": {
                "transport": "local_socket",
                "allowed_workspaces": [WORKSPACE],
                "allow_actions": allow_actions,
            }
        },
    }
    fleet.setdefault("agents", {})["pane-claude"] = {
        "managed": True,
        "mode": "herdr_session",
        "host_alias": "mac-mini",
        "workspace": WORKSPACE,
        "agent_kind": "claude",
        "transport": "local_socket",
    }
    _fleet_yaml(fleet_home).write_text(yaml.safe_dump(data))


@pytest.fixture(autouse=True)
def restore_portless_handlers():
    """Keep the module-level handler registry from leaking between tests.

    ``_PORTLESS_HANDLERS`` is process-global. A test that registers a handler
    and does not put the registry back makes the "no handler registered" case
    untestable for everything that runs after it — which is exactly what
    happened before this fixture existed.
    """
    import a2a_fleet.client as client_mod

    saved = dict(client_mod._PORTLESS_HANDLERS)
    try:
        yield
    finally:
        client_mod._PORTLESS_HANDLERS.clear()
        client_mod._PORTLESS_HANDLERS.update(saved)


@pytest.fixture
def route_env(fleet_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure(fleet_home)

    async def _capability_ok(host_alias, herdr_cfg=None, **_kw):
        return {"status": "ok", "host_alias": host_alias, "protocol": 16}

    monkeypatch.setattr(herdr_tools, "check_herdr_capability", _capability_ok)
    monkeypatch.setattr(herdr_binding, "db_path", lambda: tmp_path / "state.db")

    live: Dict[str, Any] = {"agents": [dict(SESSION_A)], "sent": []}

    async def fake_list_agents(self):
        return {"agents": [dict(a) for a in live["agents"]]}

    async def fake_get_agent(self, target):
        for agent in live["agents"]:
            if agent["terminal_id"] == target:
                return {"agent": dict(agent)}
        from a2a_fleet.herdr_client import HerdrNotFound

        raise HerdrNotFound(f"no such agent {target}")

    async def fake_send(self, target, text, **kw):
        live["sent"].append((target, text))
        return {}

    async def _supports_submit(self):
        return True

    monkeypatch.setattr(HerdrClient, "supports_submit", _supports_submit)
    monkeypatch.setattr(HerdrClient, "list_agents", fake_list_agents)
    monkeypatch.setattr(HerdrClient, "get_agent", fake_get_agent)
    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)
    return live


def _send(text: str = "continue the refactor", context_id=None) -> Dict[str, str]:
    return _run(
        herdr_receiver.send_to_herdr_session(
            "pane-claude", text, context_id=context_id
        )
    )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_dispatch_requires_confirmation_by_default(route_env) -> None:
    """Routing through fleet_send must not lower the Phase 2 bar."""
    result = _send()
    assert "confirmation required" in result["reply"]
    assert "herdr_request_action" in result["reply"]
    assert route_env["sent"] == [], "nothing may be typed into the pane yet"
    assert result["context_id"].startswith("herdr-")


def test_dispatch_sends_when_confirmation_is_disabled(
    fleet_home, route_env, monkeypatch
) -> None:
    _configure(fleet_home, require_confirmation=False)
    result = _send("run the tests")
    assert route_env["sent"] == [("term_aaa", "run the tests")]
    assert "dispatched to term_aaa" in result["reply"]
    # The receipt must not masquerade as the session's answer.
    assert "not the session's answer" in result["reply"]


def test_ambiguous_session_is_a_refusal_not_a_guess(fleet_home, route_env) -> None:
    _configure(fleet_home, require_confirmation=False)
    route_env["agents"].append(dict(SESSION_B))

    result = _send()
    assert "Refusing to choose" in result["reply"]
    assert "term_aaa" in result["reply"] and "term_bbb" in result["reply"]
    assert route_env["sent"] == []


def test_no_matching_session_reports_cleanly(fleet_home, route_env) -> None:
    _configure(fleet_home, require_confirmation=False)
    route_env["agents"].clear()
    result = _send()
    assert "no 'claude' session found" in result["reply"]
    assert route_env["sent"] == []


def test_context_id_sticks_to_its_session(fleet_home, route_env, tmp_path) -> None:
    """Continuity is by binding, so the same context reaches the same pane."""
    _configure(fleet_home, require_confirmation=False)
    first = _send("step one")
    context_id = first["context_id"]

    # A second eligible session appears — ambiguous for a NEW context, but this
    # one is already bound and must not be re-resolved.
    route_env["agents"].append(dict(SESSION_B))
    second = _send("step two", context_id=context_id)

    assert route_env["sent"] == [("term_aaa", "step one"), ("term_aaa", "step two")]
    assert second["context_id"] == context_id

    conn = herdr_binding.connect(tmp_path / "state.db")
    try:
        bound = herdr_binding.get_binding_by_context(conn, context_id)
    finally:
        conn.close()
    assert bound["terminal_id"] == "term_aaa"


def test_vanished_bound_session_refuses_to_re_home(fleet_home, route_env) -> None:
    """If the bound pane is gone, say so — do not silently pick another."""
    _configure(fleet_home, require_confirmation=False)
    context_id = _send("step one")["context_id"]

    route_env["agents"] = [dict(SESSION_B)]  # original pane closed
    result = _send("step two", context_id=context_id)

    assert "no longer available" in result["reply"]
    assert route_env["sent"] == [("term_aaa", "step one")], "no send to the new pane"


def test_takeover_blocks_routed_dispatch(fleet_home, route_env) -> None:
    _configure(fleet_home, require_confirmation=False)
    _run(
        herdr_tools.herdr_claim_human_takeover_handler(
            host_alias="mac-mini", terminal_id="term_aaa"
        )
    )
    result = _send()
    assert "held by a human" in result["reply"]
    assert route_env["sent"] == []


# ---------------------------------------------------------------------------
# Seam wiring
# ---------------------------------------------------------------------------


def test_route_registration_installs_the_portless_handler(monkeypatch) -> None:
    registered: List[Any] = []
    monkeypatch.setattr(
        herdr_receiver,
        "register_herdr_route",
        herdr_receiver.register_herdr_route,
    )
    import a2a_fleet.client as client_mod

    monkeypatch.setattr(
        client_mod,
        "register_portless_handler",
        lambda mode, handler: registered.append((mode, handler)),
    )
    herdr_receiver.register_herdr_route()
    assert registered == [("herdr_session", herdr_receiver.send_to_herdr_session)]


def test_send_message_routes_herdr_peer_through_the_handler(route_env) -> None:
    """The generic client path reaches a herdr peer without an HTTP call."""
    import a2a_fleet.client as client_mod

    seen: List[Any] = []

    async def handler(agent_name, text, **kw):
        seen.append((agent_name, text))
        return {"reply": "ok", "context_id": "ctx-1"}

    register_portless_handler("herdr_session", handler)  # restored by the fixture
    result = _run(client_mod.send_message("pane-claude", "hello"))

    assert seen == [("pane-claude", "hello")]
    assert result == {"reply": "ok", "context_id": "ctx-1"}
