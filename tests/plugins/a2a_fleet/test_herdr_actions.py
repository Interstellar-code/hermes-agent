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
from a2a_fleet.herdr_client import HerdrClient, HerdrError

SESSION = {
    "agent": "claude",
    "agent_status": "idle",
    "cwd": "/srv/workspaces/project-a",
    "pane_id": "w1:pH",
    "terminal_id": "term_abc123",
    "workspace_id": "w1",
    "revision": 40,
    # A session that has taken at least one turn. Herdr only reports
    # agent_session once the agent has actually established one, and Fleet
    # refuses to drive a session without it (see _target_ready) because a
    # freshly started agent silently swallows prompts.
    "agent_session": {"agent": "claude", "kind": "id", "value": "sess-abc"},
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


def _set_supported_kinds(fleet_home: Path, kinds: List[str]) -> None:
    """Configure the OPTIONAL per-host agent-kind allowlist."""
    data = yaml.safe_load(_fleet_yaml_path(fleet_home).read_text())
    data["fleet"]["herdr"]["hosts"]["mac-mini"]["supported_agent_kinds"] = kinds
    _fleet_yaml_path(fleet_home).write_text(yaml.safe_dump(data))


@pytest.fixture
def herdr_env(fleet_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Configured host, stubbed capability probe, temp binding store."""
    _add_host(fleet_home)

    async def _capability_ok(host_alias, herdr_cfg=None, **_kw):
        return {"status": "ok", "host_alias": host_alias, "protocol": 17}

    monkeypatch.setattr(herdr_tools, "check_herdr_capability", _capability_ok)
    monkeypatch.setattr(herdr_binding, "db_path", lambda: tmp_path / "state.db")

    live: Dict[str, Any] = {"session": dict(SESSION)}

    async def fake_get_agent(self, target):
        return {"agent": dict(live["session"])}

    async def fake_list_agents(self):
        # Resolution goes through `agent list` since herdr 0.7.5 stopped
        # accepting terminal ids as agent targets.
        return {"agents": [dict(live["session"])]}

    async def _supports_submit(self):
        return True

    # Default to a Herdr that CAN submit; the capability-gate test overrides
    # this. Without it every test would shell out to the real binary.
    monkeypatch.setattr(HerdrClient, "supports_submit", _supports_submit)
    monkeypatch.setattr(HerdrClient, "get_agent", fake_get_agent)
    monkeypatch.setattr(HerdrClient, "list_agents", fake_list_agents)
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

    async def fake_send(self, target, text, **kw):
        sent.append((target, text))
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)

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
        return {"status": "ok", "host_alias": host_alias, "protocol": 17}

    monkeypatch.setattr(herdr_tools, "check_herdr_capability", _capability_ok)
    monkeypatch.setattr(herdr_binding, "db_path", lambda: tmp_path / "state.db")

    async def fake_get_agent(self, target):
        return {"agent": dict(SESSION)}

    async def fake_list_agents(self):
        return {"agents": [dict(SESSION)]}

    monkeypatch.setattr(HerdrClient, "get_agent", fake_get_agent)
    monkeypatch.setattr(HerdrClient, "list_agents", fake_list_agents)

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

    async def fake_send(self, target, text, **kw):
        sent.append((target, text))
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)

    preview = _preview("run the tests")
    result = _request(preview["confirmation_token"])

    assert result["status"] == "submitted"
    # Addressed by pane_id: herdr 0.7.5 dropped terminal ids as agent
    # targets. terminal_id remains the identity key we resolve from.
    assert sent == [("w1:pH", "run the tests")]
    # Not complete: nothing waited for a done signal.
    assert result["completion_state"] == "pending"
    events = [row["event"] for row in _audit(tmp_path)]
    assert "submit_attempted" in events and "submitted" in events


def test_refuses_before_touching_a_session_when_herdr_cannot_submit(
    herdr_env, monkeypatch
) -> None:
    """A Herdr without `agent prompt` must be caught at the gate, not at submit.

    `agent prompt` landed in Herdr 0.7.5. Against an installed 0.7.4 this
    wrapper invoked a subcommand the binary did not have; the failure surfaced
    only after the confirmation token had been spent on a real session, leaving
    the operator to wonder whether a prompt had landed. Both tools must now
    refuse up front, and preview must not even mint a token.
    """
    calls: List[Any] = []

    async def no_submit_support(self):
        return False

    async def record_call(self, *args, **kw):
        calls.append(args)
        return {}

    monkeypatch.setattr(HerdrClient, "supports_submit", no_submit_support)
    monkeypatch.setattr(HerdrClient, "_call", record_call)

    preview = _preview()
    assert preview["status"] == "submission_unsupported"
    assert "0.7.5" in preview["required"]
    assert "confirmation_token" not in preview, "no token for a submit that cannot happen"

    request = _request("any-token")
    assert request["status"] == "submission_unsupported"
    assert calls == [], "nothing may reach Herdr once the capability check fails"


def test_stalled_submission_is_rescued_by_a_fixed_enter(
    herdr_env, tmp_path, monkeypatch
) -> None:
    """A stalled draft is completed with Enter, and the text is never re-sent.

    Herdr's own Enter (scheduled after AGENT_PROMPT_SUBMIT_DELAY) does not
    always land on a busy pane. Reproduced live 2026-07-30: identical calls
    succeeded on a quiet scratch pane and stalled elsewhere. Recovery was
    verified against a real Claude session — a planted draft submitted on
    `agent send-keys <pane> ENTER`.
    """
    sent, keys = [], []
    herdr_env["session"]["state_change_seq"] = 10

    async def stalls(self, target, text, **kw):
        sent.append(text)
        raise HerdrError("no observed state change within 5000 ms",
                         code="agent_prompt_stalled")

    async def press_enter(self, target):
        keys.append(target)
        herdr_env["session"]["state_change_seq"] = 11  # the agent moved
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", stalls)
    monkeypatch.setattr(HerdrClient, "submit_enter", press_enter)

    preview = _preview("do the thing")
    result = _request(preview["confirmation_token"])

    assert result["status"] == "submitted"
    assert keys == ["w1:pH"], "Enter is pressed on the pane, once"
    assert sent == ["do the thing"], "the prompt text is never re-sent"
    assert "submitted_after_enter" in [r["event"] for r in _audit(tmp_path)]


def test_stall_without_state_change_stays_unsubmitted(
    herdr_env, tmp_path, monkeypatch
) -> None:
    """No evidence, no success. Enter that changes nothing is not a submission."""
    herdr_env["session"]["state_change_seq"] = 10

    async def stalls(self, target, text, **kw):
        raise HerdrError("no observed state change within 5000 ms",
                         code="agent_prompt_stalled")

    async def press_enter(self, target):
        return {}  # seq deliberately unchanged

    monkeypatch.setattr(HerdrClient, "submit_prompt", stalls)
    monkeypatch.setattr(HerdrClient, "submit_enter", press_enter)
    real_sleep = asyncio.sleep  # capture before patching, or the lambda recurses
    monkeypatch.setattr(herdr_tools.asyncio, "sleep", lambda *_a, **_k: real_sleep(0))

    result = _request(_preview()["confirmation_token"])
    assert result["status"] == "draft_inserted_not_submitted"
    assert "never" in result["retry"]


def test_cli_rejection_is_not_reported_as_a_possible_draft(
    herdr_env, tmp_path, monkeypatch
) -> None:
    """An unknown subcommand means nothing ran, so nothing was typed.

    Herdr's CLI answers an unknown subcommand with its help text and exit 2.
    Treating that as draft_inserted_submission_unknown sends the operator to
    inspect a session that was never touched.
    """

    async def cli_rejects(self, target, text, **kw):
        raise RuntimeError(
            "herdr exited 2 with unparseable output: herdr agent commands:\n  herdr agent list"
        )

    monkeypatch.setattr(HerdrClient, "submit_prompt", cli_rejects)

    preview = _preview()
    result = _request(preview["confirmation_token"])

    assert result["status"] == "submission_rejected"
    assert result["mutation"].startswith("none")
    assert "submission_rejected" in [r["event"] for r in _audit(tmp_path)]


def test_submission_uses_herdrs_atomic_prompt_verb(herdr_env, monkeypatch) -> None:
    """The prompt must be submitted, not left sitting in the composer.

    The original wrapper called `agent send`, which inserts literal text and
    stops — so a confirmed action reported success while the prompt sat
    unsubmitted. This asserts the argv actually handed to Herdr: `agent prompt`,
    which writes the text AND schedules its Enter in one call
    (src/app/api/agents.rs:62).
    """
    argv: List[Any] = []

    async def record_call(self, *args, **kw):
        argv.append(args)
        return {}

    monkeypatch.setattr(HerdrClient, "_call", record_call)

    preview = _preview("continue the refactor")
    assert _request(preview["confirmation_token"])["status"] == "submitted"

    # --wait is mandatory, not optional: without it Herdr acks before the
    # Enter fires and "submitted" means nothing.
    assert argv == [(
        "agent", "prompt", "w1:pH", "continue the refactor", "--wait", "--timeout", "15000",
    )]


def test_no_arbitrary_key_injection_route_exists(herdr_env, monkeypatch) -> None:
    """The action path must not offer a way to send chosen keystrokes.

    Submission goes through Herdr's `agent prompt`, whose parameters are
    (target, text) only — there is no `keys` array to smuggle anything into.
    A caller putting key names in the action text gets them as literal text,
    not as key presses.
    """
    argv: List[Any] = []

    async def record_call(self, *args, **kw):
        argv.append(args)
        return {}

    monkeypatch.setattr(HerdrClient, "_call", record_call)

    preview = _preview("ENTER C-c ESC")
    assert _request(preview["confirmation_token"])["status"] == "submitted"

    (call,) = argv
    assert call[:2] == ("agent", "prompt")
    assert call[2] == "w1:pH", "addressed by pane_id"
    assert call[3] == "ENTER C-c ESC", "key names travel as literal text"
    assert "send-keys" not in call and "pane" not in call


def test_clean_rejection_is_not_reported_as_uncertain(herdr_env, tmp_path, monkeypatch) -> None:
    """Herdr names the failures that happened before it wrote anything.

    agent_not_ready fires before try_send_bytes, so nothing is in the composer.
    Reporting that as "may or may not have submitted" would send the operator
    to inspect a pane that was never touched.
    """

    async def not_ready(self, target, text, **kw):
        raise HerdrError("agent is no longer the pane foreground process",
                         code="agent_not_ready")

    monkeypatch.setattr(HerdrClient, "submit_prompt", not_ready)

    preview = _preview()
    result = _request(preview["confirmation_token"])

    assert result["status"] == "submission_rejected"
    assert result["herdr_error_code"] == "agent_not_ready"
    assert result["mutation"].startswith("none")
    assert "submission_rejected" in [r["event"] for r in _audit(tmp_path)]


def test_unknown_transport_failure_is_never_reported_as_submitted(
    herdr_env, tmp_path, monkeypatch
) -> None:
    """A transport death after the request leaves the outcome genuinely unknown.

    Herdr has no request ID, so a submitted prompt and a lost one look
    identical afterwards. This must never collapse into "submitted", and must
    never invite a retry that could double-submit into a live session.
    """

    async def transport_died(self, target, text, **kw):
        raise OSError("socket closed")

    monkeypatch.setattr(HerdrClient, "submit_prompt", transport_died)

    preview = _preview()
    result = _request(preview["confirmation_token"])

    assert result["status"] == "draft_inserted_submission_unknown"
    assert result["status"] != "submitted"
    assert "never" in result["retry"]
    assert "submission_unknown" in [r["event"] for r in _audit(tmp_path)]


def test_gates_block_submission_before_any_herdr_call(herdr_env, monkeypatch) -> None:
    """Takeover and allow_actions must stop the submission, not just the send.

    Both steps of the old design (text, then Enter) collapse into one call, so
    the gates only have to hold once — but they must hold before Herdr is
    touched at all.
    """
    calls: List[Any] = []

    async def record_call(self, *args, **kw):
        calls.append(args)
        return {}

    monkeypatch.setattr(HerdrClient, "_call", record_call)

    preview = _preview()
    _run(
        herdr_tools.herdr_claim_human_takeover_handler(
            host_alias="mac-mini", terminal_id="term_abc123"
        )
    )
    assert _request(preview["confirmation_token"])["status"] == "human_takeover"
    assert calls == [], "no Herdr verb may run while a human holds the session"


def test_audit_records_intent_before_the_submission(herdr_env, tmp_path, monkeypatch) -> None:
    """submit_attempted must precede submitted in the trail.

    recent_audit returns newest first, so the attempt is the LATER row. If the
    process dies between them the trail still shows an action whose outcome is
    unknown, which is the honest state.
    """

    async def fake_send(self, target, text, **kw):
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)

    preview = _preview()
    assert _request(preview["confirmation_token"])["status"] == "submitted"

    events = [r["event"] for r in _audit(tmp_path)]
    assert events.index("submitted") < events.index("submit_attempted")


def test_token_is_single_use(herdr_env, monkeypatch) -> None:
    calls: List[Any] = []

    async def fake_send(self, target, text, **kw):
        calls.append(text)
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)

    preview = _preview()
    assert _request(preview["confirmation_token"])["status"] == "submitted"
    replay = _request(preview["confirmation_token"])
    assert replay["status"] == "token_already_used"
    assert len(calls) == 1, "a replayed token must not produce a second send"


def test_takeover_is_checked_before_the_token_is_spent(
    herdr_env, tmp_path, monkeypatch
) -> None:
    """Order matters: the takeover gate runs BEFORE token consumption.

    A request made while a human holds the session must report human_takeover
    and leave the token unspent — not burn it and report token_already_used on
    the retry. Consuming a token behind a gate that refused the work would
    destroy a confirmation the operator still needs once the human is done.

    (Independent verification flagged this ordering as a mismatch against a
    test script that expected token_already_used under takeover. The ordering
    is deliberate and the safer one; this test pins it so it stays that way.)
    """
    sent: List[Any] = []

    async def fake_send(self, target, text, **kw):
        sent.append(text)
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)

    preview = _preview()
    _run(
        herdr_tools.herdr_claim_human_takeover_handler(
            host_alias="mac-mini", terminal_id="term_abc123"
        )
    )
    assert _request(preview["confirmation_token"])["status"] == "human_takeover"
    assert _request(preview["confirmation_token"])["status"] == "human_takeover"

    conn = herdr_binding.connect(tmp_path / "state.db")
    try:
        row = conn.execute(
            "SELECT consumed_at FROM herdr_tokens WHERE token = ?",
            (preview["confirmation_token"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["consumed_at"] is None, "a refused request must not spend the token"

    # And once released, that same confirmation is still good.
    _run(
        herdr_tools.herdr_release_human_takeover_handler(
            host_alias="mac-mini", terminal_id="term_abc123"
        )
    )
    assert _request(preview["confirmation_token"])["status"] == "submitted"


def test_revision_guard_reports_when_it_is_blind(herdr_env, monkeypatch) -> None:
    """A guard that cannot see must say so, not pass quietly.

    `revision` tracks terminal-title and metadata-token changes, not output
    (Herdr 0.7.4: state.rs:198, actions.rs:1083, panes.rs:1408). A session that
    never sets a title sits at revision 0 forever, so a stale token passes the
    comparison. That was verified live: a bash pane took delivered text with
    revision 0 -> 0 and a stale token was accepted.

    The delivery is not the bug — the silence would be. Both preview and send
    must label the guard blind so nobody reads a passed check as "the pane was
    idle".
    """

    async def fake_send(self, target, text, **kw):
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)

    herdr_env["session"]["revision"] = 0  # a pane that never sets a title
    preview = _preview()
    assert preview["revision_guard"].startswith("blind")

    sent = _request(preview["confirmation_token"])
    assert sent["status"] == "submitted"
    assert sent["revision_guard"].startswith("blind")


def test_revision_guard_reports_active_for_title_updating_sessions(
    herdr_env, monkeypatch
) -> None:
    async def fake_send(self, target, text, **kw):
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)

    preview = _preview()  # SESSION revision is 40
    assert preview["revision_guard"].startswith("active")
    assert _request(preview["confirmation_token"])["revision_guard"].startswith("active")


def test_unknown_token_rejected(herdr_env) -> None:
    assert _request("not-a-real-token")["status"] == "unknown_token"


def test_expired_token_rejected(herdr_env, monkeypatch) -> None:
    monkeypatch.setattr(herdr_binding, "TOKEN_TTL_SECONDS", -1)
    preview = _preview()
    assert _request(preview["confirmation_token"])["status"] == "token_expired"


def test_stale_revision_refuses_send(herdr_env, tmp_path, monkeypatch) -> None:
    """The pane moved between preview and confirmation: refuse, don't guess."""
    sent: List[Any] = []

    async def fake_send(self, target, text, **kw):
        sent.append(text)
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)

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

    async def fake_list_agents(self):
        return {"agents": [dict(herdr_env["session"]), dict(second)]}

    async def fake_send(self, target, text, **kw):
        sent.append(target)
        return {}

    monkeypatch.setattr(HerdrClient, "get_agent", fake_get_agent)
    monkeypatch.setattr(HerdrClient, "list_agents", fake_list_agents)
    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)

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
    assert _request(preview["confirmation_token"])["status"] == "submitted"


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def test_send_failure_is_unknown_not_failed_silently(herdr_env, tmp_path, monkeypatch) -> None:
    """A dropped connection leaves a truthful record and forbids retry."""

    async def boom(self, target, text, **kw):
        raise OSError("connection reset")

    monkeypatch.setattr(HerdrClient, "submit_prompt", boom)

    preview = _preview()
    result = _request(preview["confirmation_token"])

    assert result["status"] == "draft_inserted_submission_unknown"
    assert "never" in result["retry"]
    assert "submission_unknown" in [row["event"] for row in _audit(tmp_path)]


def test_wait_timeout_is_not_completion(herdr_env, tmp_path, monkeypatch) -> None:
    """Silence is never completion evidence."""

    async def fake_send(self, target, text, **kw):
        return {}

    async def timeout_wait(self, pane_id, status, timeout_ms):
        raise TimeoutError("no done signal")

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)
    monkeypatch.setattr(HerdrClient, "wait_agent_status", timeout_wait)

    preview = _preview()
    result = _request(preview["confirmation_token"], wait_timeout_ms=50)

    assert result["status"] == "submitted"
    assert result["completion_state"] == "pending"
    assert "not treated as complete" in result["completion_note"].lower()


def test_done_signal_marks_completed(herdr_env, tmp_path, monkeypatch) -> None:
    async def fake_send(self, target, text, **kw):
        return {}

    async def done_wait(self, pane_id, status, timeout_ms):
        assert status == "done", "completion must be the done signal, not idle"
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)
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

    async def fake_send(self, target, text, **kw):
        sent.append(text)
        return {}

    async def spy_call(self, *args, **kw):
        herdr_calls.append(args)
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)
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
    assert _request(preview["confirmation_token"])["status"] == "submitted"


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


# ---------------------------------------------------------------------------
# Agent-kind compatibility (codex / opencode), verified live 2026-07-31
# against herdr 0.7.5 protocol 17.
#
# These deliberately do NOT branch on agent kind. Both kinds were driven
# end-to-end through this exact code path on real panes and behaved
# identically, so a per-kind table would encode a coincidence rather than a
# difference. The tests below assert that sameness.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["claude", "codex", "opencode"])
def test_submission_path_does_not_branch_on_agent_kind(
    herdr_env, monkeypatch, kind
) -> None:
    """One code path for every Herdr agent kind.

    Herdr owns the per-runtime submission encoding inside `agent prompt`, so
    Fleet has nothing kind-specific to do. Verified live on codex and opencode.
    """
    herdr_env["session"]["agent"] = kind
    sent: List[Any] = []

    async def fake_send(self, target, text, **kw):
        sent.append((target, text))
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)

    preview = _preview("kind neutrality")
    assert preview["agent_kind"] == kind
    result = _request(preview["confirmation_token"])

    assert result["status"] == "submitted"
    assert sent == [("w1:pH", "kind neutrality")]


@pytest.mark.parametrize("kind", ["codex", "opencode"])
def test_agent_that_never_took_a_turn_is_refused_before_a_token_exists(
    herdr_env, monkeypatch, kind
) -> None:
    """Cold sessions are refused at preview, before a token is minted.

    Reproduced live 2026-07-31 on BOTH codex and opencode: seconds after
    `herdr agent start`, `agent prompt` returns success and delivers nothing.
    The pane composer stays untouched and the agent's context is unconsumed.
    `interactive_ready` is true in that window and `agent_status` reads idle,
    so the only thing that separates it from a working session is the absence
    of `agent_session`.
    """
    herdr_env["session"]["agent"] = kind
    herdr_env["session"].pop("agent_session", None)
    sent: List[Any] = []

    async def fake_send(self, target, text, **kw):
        sent.append(text)
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)

    result = _preview("must not be minted")
    assert result["status"] == "submission_target_not_ready"
    assert "confirmation_token" not in result
    assert sent == [], "nothing may be sent to a session that swallows prompts"


def test_cold_agent_is_refused_before_the_token_is_spent(
    herdr_env, monkeypatch
) -> None:
    """A token minted while warm is not spendable once the target reads cold.

    Guards the ordering: the readiness check runs BEFORE the single-use token
    is consumed, so a refused attempt does not burn it.
    """
    sent: List[Any] = []

    async def fake_send(self, target, text, **kw):
        sent.append(text)
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)

    preview = _preview("minted while warm")
    herdr_env["session"].pop("agent_session", None)  # target goes cold

    result = _request(preview["confirmation_token"])
    assert result["status"] == "submission_target_not_ready"
    assert sent == []

    # The token survived: it was never consumed by the refused attempt.
    herdr_env["session"]["agent_session"] = {"kind": "id", "value": "sess-abc"}
    assert _request(preview["confirmation_token"])["status"] == "submitted"


# ---------------------------------------------------------------------------
# agy readiness — same question, different evidence
# ---------------------------------------------------------------------------

AGY_FOOTER_WARM = "[Gemini 3.6 Flash (High)] repo git:(main) v1.1.9 conv:493d0d6f\n"
AGY_FOOTER_COLD = "[Gemini 3.6 Flash (High)] repo git:(main) v1.1.9\n"


def _agy_env(herdr_env, monkeypatch, footer: str, **overrides):
    """Point the fixture session at an agy pane and stub the pane read.

    agy reports no ``agent_session`` at any point in its life, so the shared
    readiness signal is simply absent — readiness comes from the conversation
    id in its footer instead. Returns the list of read targets so a test can
    assert the read did NOT happen.
    """
    herdr_env["session"]["agent"] = "agy"
    herdr_env["session"].pop("agent_session", None)
    herdr_env["session"]["revision"] = 0  # agy sets no terminal title
    herdr_env["session"].update(overrides)
    reads: List[Any] = []

    async def fake_read(self, target, **kw):
        reads.append(target)
        return footer

    monkeypatch.setattr(HerdrClient, "read_agent_text", fake_read)
    return reads


def test_agy_without_a_conversation_id_is_refused(herdr_env, monkeypatch) -> None:
    """A started-but-unused agy is cold, exactly like a cold codex.

    Verified live 2026-07-31 on herdr 0.7.5 / agy 1.1.9: a prompt sent ~10s
    after `agent start` (which had reported interactive_ready: true) came back
    agent_prompt_stalled with state_change_seq frozen and an empty composer.
    The same prompt landed later. Herdr never fills agent_session for agy, so
    the footer's conversation id is what separates the two states.
    """
    sent: List[Any] = []

    async def fake_send(self, target, text, **kw):
        sent.append(text)
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)
    _agy_env(herdr_env, monkeypatch, AGY_FOOTER_COLD)

    result = _preview("must not be minted")
    assert result["status"] == "submission_target_not_ready"
    assert result["agent_kind"] == "agy"
    assert "confirmation_token" not in result
    assert sent == []


def test_agy_with_a_conversation_id_submits(herdr_env, monkeypatch) -> None:
    """The absent agent_session must not block an agy that has taken a turn."""
    sent: List[Any] = []

    async def fake_send(self, target, text, **kw):
        sent.append(text)
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)
    _agy_env(herdr_env, monkeypatch, AGY_FOOTER_WARM)

    preview = _preview("run the tests")
    assert preview["status"] == "preview"
    assert _request(preview["confirmation_token"])["status"] == "submitted"
    assert sent == ["run the tests"]


def test_agy_with_unsettled_detection_is_refused_without_reading(
    herdr_env, monkeypatch
) -> None:
    """`unknown` status means detection has not settled — refuse it first.

    Also pins the ordering: the cheap field check runs before the extra pane
    read, so a session Herdr cannot even classify costs no subprocess.
    """
    reads = _agy_env(
        herdr_env, monkeypatch, AGY_FOOTER_WARM, agent_status="unknown"
    )
    result = _preview()
    assert result["status"] == "submission_target_not_ready"
    assert reads == []


def test_agy_reported_not_interactive_is_refused(herdr_env, monkeypatch) -> None:
    """interactive_ready is a veto when present, never a requirement.

    Herdr only reports it for agents it started itself, so requiring it would
    refuse every operator-started pane — which is every pane Fleet exists for.
    """
    _agy_env(herdr_env, monkeypatch, AGY_FOOTER_WARM, interactive_ready=False)
    assert _preview()["status"] == "submission_target_not_ready"


def test_agy_unreadable_pane_fails_closed(herdr_env, monkeypatch) -> None:
    """No evidence is not the same as good evidence."""

    async def fake_read(self, target, **kw):
        raise HerdrError("pane is gone", code="pane_not_found")

    _agy_env(herdr_env, monkeypatch, AGY_FOOTER_WARM)
    monkeypatch.setattr(HerdrClient, "read_agent_text", fake_read)
    assert _preview()["status"] == "submission_target_not_ready"


def test_agy_readiness_is_rechecked_before_the_token_is_spent(
    herdr_env, monkeypatch
) -> None:
    """A token minted against a warm agy is not spendable once it reads cold."""
    sent: List[Any] = []
    footer = {"text": AGY_FOOTER_WARM}

    async def fake_send(self, target, text, **kw):
        sent.append(text)
        return {}

    async def fake_read(self, target, **kw):
        return footer["text"]

    _agy_env(herdr_env, monkeypatch, AGY_FOOTER_WARM)
    monkeypatch.setattr(HerdrClient, "read_agent_text", fake_read)
    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)

    preview = _preview("minted while warm")
    footer["text"] = AGY_FOOTER_COLD  # e.g. the session was restarted

    result = _request(preview["confirmation_token"])
    assert result["status"] == "submission_target_not_ready"
    assert sent == []

    # The token survived the refusal: it was never consumed.
    footer["text"] = AGY_FOOTER_WARM
    assert _request(preview["confirmation_token"])["status"] == "submitted"


def test_wait_timeout_with_state_movement_is_submitted_not_unknown(
    herdr_env, monkeypatch
) -> None:
    """Herdr's wait budget expiring is not evidence the prompt failed.

    Verified live 2026-07-31: opencode exceeded a 15s `--wait` on a submission
    that HAD landed (state_change_seq 76 -> 79, reply in the pane), and the
    call came back `timeout`. Reporting that as an unknown outcome is the
    expensive kind of wrong — unknown is never retried, so a good submission
    turns into an operator investigation.
    """
    herdr_env["session"]["state_change_seq"] = 10
    keys: List[Any] = []

    async def times_out(self, target, text, **kw):
        # The agent did move; Herdr just stopped watching before it settled.
        herdr_env["session"]["state_change_seq"] = 11
        raise HerdrError("timed out waiting for agent status", code="timeout")

    async def press_enter(self, target):
        keys.append(target)
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", times_out)
    monkeypatch.setattr(HerdrClient, "submit_enter", press_enter)

    preview = _preview("slow agent")
    result = _request(preview["confirmation_token"])

    assert result["status"] == "submitted"
    assert keys == [], (
        "a wait timeout must not press Enter: the prompt may already be in "
        "flight, and Enter would submit a second time"
    )


def test_wait_timeout_without_state_movement_falls_back_to_enter(
    herdr_env, monkeypatch
) -> None:
    """No movement after a wait timeout is treated exactly like a stall."""
    herdr_env["session"]["state_change_seq"] = 10
    keys: List[Any] = []

    async def times_out(self, target, text, **kw):
        raise HerdrError("timed out waiting for agent status", code="timeout")

    async def press_enter(self, target):
        keys.append(target)
        herdr_env["session"]["state_change_seq"] = 11  # Enter took
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", times_out)
    monkeypatch.setattr(HerdrClient, "submit_enter", press_enter)

    preview = _preview("stalled and slow")
    result = _request(preview["confirmation_token"])

    assert result["status"] == "submitted"
    assert keys == ["w1:pH"], "Enter is pressed once, on the pane"


def test_unlisted_kind_passes_when_no_allowlist_is_configured(
    herdr_env, monkeypatch
) -> None:
    """No allowlist means no restriction — the key is opt-in, not opt-out."""
    herdr_env["session"]["agent"] = "some-future-kind"
    async def fake_send(self, target, text, **kw):
        return {}

    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)
    assert _preview("unrestricted")["status"] == "preview"


def test_configured_allowlist_refuses_an_unlisted_kind(
    fleet_home, tmp_path, monkeypatch
) -> None:
    """An operator can decline to treat every future Herdr kind as proven."""
    _add_host(fleet_home)
    _set_supported_kinds(fleet_home, ["claude", "codex"])

    async def _capability_ok(host_alias, herdr_cfg=None, **_kw):
        return {"status": "ok", "host_alias": host_alias, "protocol": 17}

    monkeypatch.setattr(herdr_tools, "check_herdr_capability", _capability_ok)
    monkeypatch.setattr(herdr_binding, "db_path", lambda: tmp_path / "state.db")

    session = dict(SESSION, agent="opencode")

    async def fake_get_agent(self, target):
        return {"agent": dict(session)}

    async def fake_list_agents(self):
        return {"agents": [dict(session)]}

    async def _supports_submit(self):
        return True

    sent: List[Any] = []

    async def fake_send(self, target, text, **kw):
        sent.append(text)
        return {}

    monkeypatch.setattr(HerdrClient, "supports_submit", _supports_submit)
    monkeypatch.setattr(HerdrClient, "get_agent", fake_get_agent)
    monkeypatch.setattr(HerdrClient, "list_agents", fake_list_agents)
    monkeypatch.setattr(HerdrClient, "submit_prompt", fake_send)

    result = _preview("blocked by allowlist")
    assert result["status"] == "agent_kind_not_allowed"
    assert result["agent_kind"] == "opencode"
    assert sent == []


def test_verified_kinds_are_the_ones_driven_end_to_end() -> None:
    """Documentation guard: the recorded list is what was actually tested."""
    assert set(herdr_tools.VERIFIED_AGENT_KINDS) == {
        "claude",
        "codex",
        "opencode",
        # agy joined on 2026-07-31: full gate matrix driven against a scratch
        # pane (discovery, exact-id inspect, preview, confirmed submit, marker
        # observed in the pane, replay refused, takeover blocking both verbs,
        # audit ordering) plus the cold-target refusal on a second pane.
        "agy",
    }
