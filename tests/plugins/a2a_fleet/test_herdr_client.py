"""HerdrClient: envelope parsing, error mapping, timeouts, argv building.

Subprocess is faked throughout — these tests must not depend on a live
``herdr`` binary.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import List, Optional

import pytest


class _FakeStream:
    """Mimics asyncio.StreamReader.read() with one chunk, or hangs forever."""

    def __init__(self, data: bytes = b"", hang: bool = False) -> None:
        self._data = data
        self._hang = hang
        self._sent = False

    async def read(self, _n: int = -1) -> bytes:
        if self._hang:
            await asyncio.sleep(999)
        if self._sent:
            return b""
        self._sent = True
        return self._data


class _FakeProc:
    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        hang: bool = False,
    ) -> None:
        self.stdout = _FakeStream(stdout, hang=hang)
        self.stderr = _FakeStream(stderr)
        self._returncode = returncode
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self._returncode


def _patch_exec(monkeypatch: pytest.MonkeyPatch, proc: _FakeProc, captured: Optional[List] = None):
    async def fake_create_subprocess_exec(*argv, **kwargs):
        if captured is not None:
            captured.append(list(argv))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)


def _client(monkeypatch: pytest.MonkeyPatch, **kwargs):
    from a2a_fleet.herdr_client import HerdrClient

    monkeypatch.setattr("a2a_fleet.herdr_client.shutil.which", lambda _name: "/usr/local/bin/herdr")
    return HerdrClient(**kwargs)


def test_envelope_result_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "id": "cli:agent:list",
        "result": {"type": "agent_list", "agents": [{"terminal_id": "term_1"}]},
    }
    _patch_exec(monkeypatch, _FakeProc(stdout=json.dumps(body).encode(), returncode=0))
    client = _client(monkeypatch)

    result = asyncio.run(client.list_agents())
    assert result["agents"][0]["terminal_id"] == "term_1"


def test_envelope_error_shape_maps_to_exception_subclass(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.herdr_client import HerdrNotFound

    body = {
        "id": "cli:agent:get",
        "error": {"code": "agent_not_found", "message": "agent target xyz not found"},
    }
    _patch_exec(monkeypatch, _FakeProc(stdout=json.dumps(body).encode(), returncode=1))
    client = _client(monkeypatch)

    with pytest.raises(HerdrNotFound) as exc:
        asyncio.run(client.get_agent("xyz"))
    assert exc.value.code == "agent_not_found"


def test_unknown_error_code_raises_base_herdr_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.herdr_client import HerdrError, HerdrNotFound

    body = {"id": "cli:agent:get", "error": {"code": "something_else", "message": "boom"}}
    _patch_exec(monkeypatch, _FakeProc(stdout=json.dumps(body).encode(), returncode=1))
    client = _client(monkeypatch)

    with pytest.raises(HerdrError) as exc:
        asyncio.run(client.get_agent("xyz"))
    assert not isinstance(exc.value, HerdrNotFound)
    assert exc.value.code == "something_else"


def test_non_zero_exit_unparseable_output_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from a2a_fleet.herdr_client import HerdrUnavailable

    _patch_exec(
        monkeypatch,
        _FakeProc(stdout=b"not json at all", stderr=b"herdr: no server running", returncode=1),
    )
    client = _client(monkeypatch)

    with pytest.raises(HerdrUnavailable):
        asyncio.run(client.status())


def test_timeout_raises_herdr_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.herdr_client import HerdrTimeout

    _patch_exec(monkeypatch, _FakeProc(hang=True))
    client = _client(monkeypatch, timeout=0.05)

    with pytest.raises(HerdrTimeout):
        asyncio.run(client.status())


def test_oversized_output_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.herdr_client import HerdrUnavailable

    _patch_exec(monkeypatch, _FakeProc(stdout=b"x" * (10 * 1024 * 1024 + 1), returncode=0))
    client = _client(monkeypatch)

    with pytest.raises(HerdrUnavailable):
        asyncio.run(client.status())


def test_protocol_match_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)

    async def fake_schema():
        return {"protocol": 17, "schema_version": 1}

    monkeypatch.setattr(client, "schema", fake_schema)
    asyncio.run(client.check_protocol())  # must not raise


def test_protocol_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.herdr_client import HerdrClient, HerdrProtocolMismatch

    client = _client(monkeypatch)

    async def fake_schema():
        return {"protocol": 15, "schema_version": 1}

    monkeypatch.setattr(client, "schema", fake_schema)
    with pytest.raises(HerdrProtocolMismatch) as exc:
        asyncio.run(client.check_protocol())
    assert str(HerdrClient.PROTOCOL_VERSION) in str(exc.value)
    assert "15" in str(exc.value)


def test_remote_argv_gets_prefix_in_right_position(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, binary="herdr", ssh_target="mac-mini")
    assert client._build_argv("status", "--json") == [
        "herdr",
        "--remote",
        "mac-mini",
        "status",
        "--json",
    ]


def test_local_argv_has_no_remote_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, binary="herdr")
    assert client._build_argv("status", "--json") == ["herdr", "status", "--json"]


def test_normalize_picks_terminal_id_and_carries_pane_id_and_revision() -> None:
    from a2a_fleet.herdr_client import normalize_agent_record

    raw = {
        "agent": "claude",
        "agent_status": "working",
        "cwd": "/Users/rohits/.hermes/hermes-agent",
        "foreground_cwd": "/Users/rohits/.claude/plugins/cache/context-mode/1.0.169",
        "pane_id": "w6523b0e28fea48:pH",
        "terminal_id": "term_656fd55fd56381a",
        "workspace_id": "w6523b0e28fea48",
        "revision": 36,
        "terminal_title_stripped": "Approve pending update",
        "focused": True,
    }

    normalized = normalize_agent_record(raw)
    assert normalized["terminal_id"] == "term_656fd55fd56381a"
    assert normalized["pane_id"] == "w6523b0e28fea48:pH"
    assert normalized["revision"] == 36
    assert normalized["agent_kind"] == "claude"


def test_normalize_ignores_foreground_cwd() -> None:
    """Real-world trap: cwd and foreground_cwd diverge; foreground_cwd must
    never leak into the normalized record or be used as workspace identity."""
    from a2a_fleet.herdr_client import normalize_agent_record

    raw = {
        "agent": "claude",
        "cwd": "/Users/rohits/.hermes/hermes-agent",
        "foreground_cwd": "/Users/rohits/.claude/plugins/cache/context-mode/1.0.169",
        "pane_id": "p1",
        "terminal_id": "term_1",
        "workspace_id": "w1",
        "revision": 1,
    }

    normalized = normalize_agent_record(raw)
    assert normalized["cwd"] == "/Users/rohits/.hermes/hermes-agent"
    assert "foreground_cwd" not in normalized


def test_error_envelope_on_stderr_with_nonzero_exit_is_parsed() -> None:
    """Regression: herdr writes FAILURE envelopes to stderr and exits non-zero.

    Found live — `herdr agent get term_doesnotexist` exits 1 with an EMPTY stdout
    and the {"error":{"code":"agent_not_found"}} envelope on stderr. Parsing only
    stdout collapsed every structured Herdr error into an opaque
    HerdrUnavailable, losing the error code (so callers saw "herdr_error" instead
    of "not_found"). The unit tests all mocked exit 0, which is why this survived.
    """
    from a2a_fleet.herdr_client import HerdrClient, HerdrNotFound

    client = HerdrClient(binary="/nonexistent/herdr")

    async def fake_exec(argv, timeout=None):
        return (
            1,
            b"",
            b'{"id":"cli:agent:get","error":{"code":"agent_not_found",'
            b'"message":"agent target term_x not found"}}',
        )

    client._exec = fake_exec  # type: ignore[assignment]

    with pytest.raises(HerdrNotFound):
        asyncio.run(client.get_agent("term_x"))


def test_herdr_client_does_not_import_pane_parsing() -> None:
    """Enforce the no-scraping non-goal: no pane-content/screen-state verbs."""
    import a2a_fleet.herdr_client as herdr_client

    source = Path(inspect.getfile(herdr_client)).read_text()
    # "pane send-keys"/"send-text" stay banned: pane-level keystrokes bypass
    # the agent-session identity boundary. The agent-scoped
    # `agent send-keys <target> ENTER` IS allowed and deliberately narrow —
    # the key is hardcoded, there is no parameter, and it only commits text
    # Herdr has already confirmed is in the composer. Added 2026-07-30 after
    # Herdr's own scheduled Enter was shown to drop on busy panes.
    banned = ["pane read", "pane send-keys", "send-text", "screen_state", "pane run"]
    for phrase in banned:
        assert phrase not in source, f"herdr_client.py must not reference {phrase!r}"


def test_submit_prompt_subprocess_budget_outlasts_the_wait_budget() -> None:
    """The wrapper must not kill the call it is waiting on.

    `submit_prompt` asks Herdr to wait `verify_ms` for an observable state
    change, but the subprocess timeout defaulted to 10s. Verified live
    2026-07-31: opencode's submission ran past 10s inside a 15s `--wait`, this
    wrapper killed it, and a prompt that HAD landed was reported as
    draft_inserted_submission_unknown — an outcome that is never retried.
    """
    import asyncio

    from a2a_fleet.herdr_client import HerdrClient

    seen = {}

    async def fake_exec(argv, timeout=None):
        seen["argv"] = argv
        seen["timeout"] = timeout
        return (0, b'{"result": {}}', b"")

    client = HerdrClient(binary="/nonexistent/herdr")
    client._exec = fake_exec  # type: ignore[method-assign]
    asyncio.run(client.submit_prompt("w1:pH", "hello", verify_ms=15000))

    wait_budget_s = 15.0
    assert seen["timeout"] is not None
    assert seen["timeout"] > wait_budget_s, (
        f"subprocess budget {seen['timeout']}s must outlast the --wait budget "
        f"{wait_budget_s}s it just asked Herdr for"
    )
