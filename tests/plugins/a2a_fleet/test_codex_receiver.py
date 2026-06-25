"""Unit tests for the standalone Codex receiver template.

The template lives under ``plugins/a2a_fleet/templates/codex_receiver.py`` and is a
standalone script (not part of the importable package). Load it by path so we
can exercise its helpers without spawning a real ``codex`` CLI or network.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[3]
    / "plugins" / "a2a_fleet" / "templates" / "codex_receiver.py"
)


@pytest.fixture(scope="module")
def cxr():
    spec = importlib.util.spec_from_file_location("codex_receiver_under_test", TEMPLATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _isolate_receiver_runtime_paths(cxr, tmp_path, monkeypatch):
    runtime = tmp_path / "receiver-runtime"
    runtime.mkdir()
    for attr, name in (
        ("CONFIG_PATH", "codex_receiver.json"),
        ("INBOX_PATH", "a2a-codex-inbox.jsonl"),
        ("INBOX_OFFSET_PATH", "a2a-codex-inbox.offset"),
        ("TRANSCRIPT_PATH", "a2a-codex-transcript.jsonl"),
        ("PID_PATH", "codex_receiver.pid"),
        ("TOKEN_PATH", ".codex-token"),
        ("SESSION_MAP_PATH", "a2a-codex-sessions.json"),
    ):
        monkeypatch.setattr(cxr, attr, runtime / name, raising=False)


def _base_cfg(cxr, repo: Path) -> dict:
    cfg = dict(cxr.DEFAULTS)
    cfg["repo_path"] = str(repo)
    cfg["role_prompt"] = "ROLE-PROMPT-MARKER"
    cfg["codex_model"] = "o4-mini"
    return cfg


def test_codex_receiver_template_compiles() -> None:
    import py_compile
    py_compile.compile(str(TEMPLATE_PATH), doraise=True)


def test_isolated_runtime_filenames(cxr) -> None:
    assert cxr.CONFIG_PATH.name == "codex_receiver.json"
    assert cxr.INBOX_PATH.name == "a2a-codex-inbox.jsonl"
    assert cxr.INBOX_OFFSET_PATH.name == "a2a-codex-inbox.offset"
    assert cxr.TRANSCRIPT_PATH.name == "a2a-codex-transcript.jsonl"
    assert cxr.PID_PATH.name == "codex_receiver.pid"
    assert cxr.TOKEN_PATH.name == ".codex-token"
    assert cxr.SESSION_MAP_PATH.name == "a2a-codex-sessions.json"


def test_session_map_round_trip(cxr) -> None:
    path = cxr.SESSION_MAP_PATH
    assert cxr.get_thread_id_for_context("ctx-1", path) is None
    cxr.store_thread_id_for_context("ctx-1", "thread_abc", path)
    assert cxr.get_thread_id_for_context("ctx-1", path) == "thread_abc"
    raw = json.loads(path.read_text())
    assert raw["ctx-1"]["thread_id"] == "thread_abc"
    assert isinstance(raw["ctx-1"]["updated_at"], int)


def test_build_command_first_turn_no_thread(cxr, tmp_path) -> None:
    cfg = _base_cfg(cxr, tmp_path)

    first = cxr.build_codex_command("do it", cfg, thread_id=None)
    assert first[:2] == ["codex", "exec"]
    assert "resume" not in first
    assert "--json" in first
    assert "--skip-git-repo-check" in first
    assert "-s" in first
    assert "workspace-write" in first
    # No --color (asymmetry with exec resume)
    assert "--color" not in first


def test_build_command_resume_turn(cxr, tmp_path) -> None:
    cfg = _base_cfg(cxr, tmp_path)

    resume = cxr.build_codex_command("next step", cfg, thread_id="thread-xyz")
    assert resume[:4] == ["codex", "exec", "resume", "thread-xyz"]
    assert "--json" in resume
    assert "--skip-git-repo-check" in resume
    # No -s/--sandbox on resume (inherits from thread creation)
    assert "-s" not in resume
    assert "--sandbox" not in resume
    # No --color
    assert "--color" not in resume


def test_build_command_model_flag(cxr, tmp_path) -> None:
    cfg = _base_cfg(cxr, tmp_path)
    cmd = cxr.build_codex_command("hi", cfg, thread_id=None)
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "o4-mini"


def test_build_command_no_model(cxr, tmp_path) -> None:
    cfg = _base_cfg(cxr, tmp_path)
    cfg["codex_model"] = None
    cmd = cxr.build_codex_command("hi", cfg, thread_id=None)
    assert "-m" not in cmd


# ---------------------------------------------------------------------------
# JSONL output parsing
# ---------------------------------------------------------------------------

def test_parse_codex_output_captures_thread_id_and_last_agent_message(cxr) -> None:
    """thread.started -> thread_id; last item.completed agent_message -> reply."""
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "tid-abc"}),
        json.dumps({"type": "turn.started"}),
        # Tool call item — should be skipped (not agent_message)
        json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "tool_call", "text": "ignored"}}),
        # First agent message
        json.dumps({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "first reply"}}),
        # Second agent message — LAST one wins
        json.dumps({"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "final reply"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}),
    ])
    thread_id, reply = cxr.parse_codex_output(stdout)
    assert thread_id == "tid-abc"
    assert reply == "final reply"


def test_parse_codex_output_multiple_item_completed_last_agent_message_wins(cxr) -> None:
    """When multiple item.completed events exist, the LAST agent_message is the reply."""
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "tid-multi"}),
        json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "step one"}}),
        json.dumps({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "step two"}}),
        json.dumps({"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "step three"}}),
    ])
    thread_id, reply = cxr.parse_codex_output(stdout)
    assert thread_id == "tid-multi"
    assert reply == "step three"


def test_parse_codex_output_no_agent_message_returns_none(cxr) -> None:
    """When no agent_message item exists, reply is None."""
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "tid-notext"}),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ])
    thread_id, reply = cxr.parse_codex_output(stdout)
    assert thread_id == "tid-notext"
    assert reply is None


def test_parse_codex_output_turn_completed_has_no_text(cxr) -> None:
    """turn.completed only has usage — do NOT read reply from it."""
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "tid-usage"}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}}),
    ])
    thread_id, reply = cxr.parse_codex_output(stdout)
    assert thread_id == "tid-usage"
    assert reply is None


def test_parse_codex_output_empty_stdout(cxr) -> None:
    thread_id, reply = cxr.parse_codex_output("")
    assert thread_id is None
    assert reply is None


def test_parse_codex_output_ignores_malformed_lines(cxr) -> None:
    stdout = "\n".join([
        "not json at all",
        json.dumps({"type": "thread.started", "thread_id": "tid-ok"}),
        "also bad {{{",
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}),
    ])
    thread_id, reply = cxr.parse_codex_output(stdout)
    assert thread_id == "tid-ok"
    assert reply == "hi"


# ---------------------------------------------------------------------------
# Session-not-found detection (checks BOTH reply and stderr)
# ---------------------------------------------------------------------------

def test_is_session_not_found_detects_in_reply(cxr) -> None:
    assert cxr._is_session_not_found(
        "error: no rollout found for thread id abc123",
        "",
    ) is True


def test_is_session_not_found_detects_in_stderr(cxr) -> None:
    assert cxr._is_session_not_found(
        None,
        "JSON-RPC -32600: no rollout found for thread id xyz",
    ) is True


def test_is_session_not_found_false_on_unrelated_error(cxr) -> None:
    assert cxr._is_session_not_found("some unrelated error text", "stderr noise") is False


def test_is_session_not_found_false_on_none_and_empty(cxr) -> None:
    assert cxr._is_session_not_found(None, "") is False


# ---------------------------------------------------------------------------
# run_codex_turn: first-turn captures thread_id, second turn resumes
# ---------------------------------------------------------------------------

def test_run_codex_turn_persists_then_resumes_thread(cxr, tmp_path, monkeypatch) -> None:
    cfg = _base_cfg(cxr, tmp_path)
    calls = []
    stored = {}

    monkeypatch.setattr(cxr, "get_thread_id_for_context", lambda context_id, path=None: stored.get(context_id))
    monkeypatch.setattr(
        cxr, "store_thread_id_for_context",
        lambda context_id, thread_id, path=None: stored.__setitem__(context_id, thread_id),
    )

    def fake_runner(cmd, cwd, timeout):
        calls.append(cmd)
        if stored.get("ctx-1") is not None:
            # Resume turn
            return (
                "\n".join([
                    json.dumps({"type": "thread.started", "thread_id": "tid-first"}),
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "turn-2"}}),
                ]),
                0,
                "",
            )
        # First turn
        return (
            "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "tid-first"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "turn-1"}}),
            ]),
            0,
            "",
        )

    first = cxr.run_codex_turn("turn one", "ctx-1", cfg, runner=fake_runner)
    second = cxr.run_codex_turn("turn two", "ctx-1", cfg, runner=fake_runner)

    assert first == "turn-1"
    assert second == "turn-2"
    assert stored["ctx-1"] == "tid-first"
    # First turn: no resume
    assert "resume" not in calls[0]
    # Second turn: resume with the stored thread_id
    assert "resume" in calls[1]
    assert calls[1][calls[1].index("resume") + 1] == "tid-first"


def test_run_codex_turn_remints_when_stored_thread_missing(cxr, tmp_path, monkeypatch) -> None:
    cfg = _base_cfg(cxr, tmp_path)
    calls = []
    stored = {"ctx-1": "tid-dead"}

    monkeypatch.setattr(cxr, "get_thread_id_for_context", lambda context_id, path=None: stored.get(context_id))
    monkeypatch.setattr(
        cxr, "store_thread_id_for_context",
        lambda context_id, thread_id, path=None: stored.__setitem__(context_id, thread_id),
    )

    def fake_runner(cmd, cwd, timeout):
        calls.append(cmd)
        if "resume" in cmd:
            # Dead thread signal in stderr
            return ("", 1, "error: no rollout found for thread id tid-dead")
        return (
            "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "tid-new"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "fresh"}}),
            ]),
            0,
            "",
        )

    reply = cxr.run_codex_turn("redo", "ctx-1", cfg, runner=fake_runner)
    assert reply == "fresh"
    assert stored["ctx-1"] == "tid-new"
    # First call was a resume (stored thread), second was a fresh first turn
    assert "resume" in calls[0]
    assert "resume" not in calls[1]


def test_run_codex_turn_remints_when_dead_thread_in_reply(cxr, tmp_path, monkeypatch) -> None:
    """Remint fires when the session-not-found signal appears in parsed reply text, not stderr.

    This test FAILS before the fix (_is_session_not_found only checked one source)
    and PASSES after (it checks both reply and stderr).
    """
    cfg = _base_cfg(cxr, tmp_path)
    calls = []
    stored = {"ctx-2": "tid-dead"}

    monkeypatch.setattr(cxr, "get_thread_id_for_context", lambda context_id, path=None: stored.get(context_id))
    monkeypatch.setattr(
        cxr, "store_thread_id_for_context",
        lambda context_id, thread_id, path=None: stored.__setitem__(context_id, thread_id),
    )

    def fake_runner(cmd, cwd, timeout):
        calls.append(cmd)
        if "resume" in cmd:
            # Dead-thread signal in reply text (stdout), NOT stderr
            return (
                "\n".join([
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                                "text": "no rollout found for thread id tid-dead"}}),
                ]),
                0,
                "",
            )
        return (
            "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "tid-fresh"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "reminted"}}),
            ]),
            0,
            "",
        )

    reply = cxr.run_codex_turn("test", "ctx-2", cfg, runner=fake_runner)
    assert reply == "reminted", (
        f"Expected reminted reply, got {reply!r} — "
        "remint did not fire on reply-text session signal"
    )
    assert stored["ctx-2"] == "tid-fresh"
    assert len(calls) == 2
    assert "resume" in calls[0]
    assert "resume" not in calls[1]


# ---------------------------------------------------------------------------
# Bearer auth on the codex receiver HTTP layer
# ---------------------------------------------------------------------------

class _FakeRfile:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, n):
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


class _FakeWfile:
    def __init__(self):
        self.buf = b""

    def write(self, b):
        self.buf += b


def _make_codex_request(cxr, cfg, token, *, headers, body=b"", path="/jsonrpc", method="POST"):
    """Drive a Handler instance without a real socket."""
    HandlerCls = cxr.make_handler(cfg, token, None)

    class H(HandlerCls):
        def __init__(self):  # bypass BaseHTTPRequestHandler.__init__ (no socket)
            self.headers = headers
            self.path = path
            self.command = method
            self.rfile = _FakeRfile(body)
            self.wfile = _FakeWfile()
            self.requestline = ""
            self.client_address = ("127.0.0.1", 0)
            self._status = None

        def send_response(self, code, message=None):
            self._status = code

        def send_header(self, *a, **k):
            pass

        def end_headers(self):
            pass

    h = H()
    return h


def test_codex_receiver_bearer_auth_wrong_token_returns_401(cxr, tmp_path) -> None:
    cfg = _base_cfg(cxr, tmp_path)
    h = _make_codex_request(cxr, cfg, "correct-token",
                            headers={"Authorization": "Bearer wrong-token",
                                     "Content-Length": "2"},
                            body=b"{}")
    h.do_POST()
    assert h._status == 401


def test_codex_receiver_bearer_auth_missing_token_returns_401(cxr, tmp_path) -> None:
    cfg = _base_cfg(cxr, tmp_path)
    h = _make_codex_request(cxr, cfg, "correct-token",
                            headers={"Content-Length": "2"},
                            body=b"{}")
    h.do_POST()
    assert h._status == 401


def test_codex_receiver_bearer_auth_correct_token_is_accepted(cxr, tmp_path) -> None:
    cfg = _base_cfg(cxr, tmp_path)
    body = json.dumps({
        "jsonrpc": "2.0", "id": "1", "method": "SendMessage",
        "params": {"message": {"contextId": "ctx-x", "parts": [{"text": "hi"}]}},
    }).encode()
    h = _make_codex_request(cxr, cfg, "correct-token",
                            headers={"Authorization": "Bearer correct-token",
                                     "Content-Length": str(len(body))},
                            body=body)
    h.do_POST()
    assert h._status != 401, "Correct token was rejected with 401"


# ---------------------------------------------------------------------------
# Fix 1: remint clears stale thread_id even when fresh retry yields no thread.started
# ---------------------------------------------------------------------------

def test_remint_clears_stale_thread_id_when_fresh_retry_yields_no_new_id(cxr, tmp_path) -> None:
    """Remint path removes the dead thread_id from the session map BEFORE retrying.

    If the fresh retry also emits no thread.started, the map must have NO entry
    for that contextId — the bad id must NOT be re-persisted.

    This test FAILS before Fix 1 (stale id stays on disk) and PASSES after.
    """
    cfg = _base_cfg(cxr, tmp_path)
    session_map_path = cxr.SESSION_MAP_PATH

    # Seed a dead thread_id into the on-disk session map.
    cxr.store_thread_id_for_context("ctx-stale", "tid-dead", session_map_path)
    assert cxr.get_thread_id_for_context("ctx-stale", session_map_path) == "tid-dead"

    def fake_runner(cmd, cwd, timeout):
        if "resume" in cmd:
            # Resume attempt → session-not-found signal
            return ("", 1, "error: no rollout found for thread id tid-dead")
        # Fresh first-turn attempt → succeeds but emits NO thread.started
        # (simulates a remint retry that completes without a thread.started frame)
        return (
            "\n".join([
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}),
            ]),
            0,
            "",
        )

    reply = cxr.run_codex_turn("redo", "ctx-stale", cfg, runner=fake_runner)

    # The stale id must be gone — not re-persisted after the failed remint.
    stored = cxr.get_thread_id_for_context("ctx-stale", session_map_path)
    assert stored is None, (
        f"Expected no thread_id after failed remint, but found {stored!r} — "
        "Fix 1: clear_thread_id_for_context must be called before the retry"
    )
    # The reply should still be returned (partial output from the retry).
    assert reply == "partial"


# ---------------------------------------------------------------------------
# Fix 2: forbidden flags in codex_extra_flags are sanitized on resume and first turn
# ---------------------------------------------------------------------------

def test_forbidden_extra_flags_stripped_on_resume_and_ephemeral_on_first_turn(cxr, tmp_path) -> None:
    """codex_extra_flags with forbidden flags are stripped before appending to the command.

    Resume command: --color/value, -s/value, --ephemeral must be removed; --foo bar must survive.
    First-turn command: --ephemeral must be removed; --color and -s are allowed.

    This test verifies Fix 2.
    """
    cfg = _base_cfg(cxr, tmp_path)
    cfg["codex_extra_flags"] = ["--color", "never", "-s", "danger-full-access", "--ephemeral", "--foo", "bar"]

    # --- RESUME command ---
    resume_cmd = cxr.build_codex_command("do work", cfg, thread_id="tid-xyz")

    # Forbidden tokens must not appear anywhere in the resume command.
    assert "--color" not in resume_cmd, "--color must be stripped from resume command"
    assert "never" not in resume_cmd, "value 'never' of --color must be stripped from resume command"
    assert "-s" not in resume_cmd, "-s must be stripped from resume command"
    assert "danger-full-access" not in resume_cmd, "value of -s must be stripped from resume command"
    assert "--ephemeral" not in resume_cmd, "--ephemeral must be stripped from resume command"

    # Allowed extra flags must survive.
    assert "--foo" in resume_cmd, "--foo must be retained in resume command"
    assert "bar" in resume_cmd, "value 'bar' of --foo must be retained in resume command"

    # --- FIRST-TURN command ---
    first_cmd = cxr.build_codex_command("do work", cfg, thread_id=None)

    # --ephemeral must be stripped from first-turn too (breaks resume model).
    assert "--ephemeral" not in first_cmd, "--ephemeral must be stripped from first-turn command"

    # --color and -s are allowed on first turn — they should survive.
    assert "--color" in first_cmd, "--color should be retained on first-turn command"
    assert "--foo" in first_cmd, "--foo must be retained in first-turn command"
    assert "bar" in first_cmd, "value 'bar' of --foo must be retained in first-turn command"


def test_main_fails_closed_for_non_loopback_without_auth(cxr, monkeypatch, tmp_path) -> None:
    cfg = _base_cfg(cxr, tmp_path)
    cfg["bind_host"] = "0.0.0.0"
    monkeypatch.setattr(cxr, "load_config", lambda config_path=None: cfg)
    monkeypatch.setattr(cxr, "resolve_auth_token", lambda c: None)
    rc = cxr.main()
    assert rc == 2, "Expected exit code 2 for non-loopback bind without auth"
