"""Unit tests for the standalone agy (Antigravity CLI) receiver template.

The template lives under ``plugins/a2a_fleet/templates/agy_receiver.py`` and is a
standalone script (not part of the importable package). Load it by path so we
can exercise its helpers without spawning a real ``agy`` CLI or network.

The transcript-tail samples below are the EXACT bytes observed when probing agy
v1.0.4 in /tmp/agy-probe (see the template docstring for the capture):
  turn1 stdout: "remembered the word BANANA.\n"
  turn2 stdout: "remembered the word BANANA.\nBANANA\n"
  turn3 stdout: "remembered the word BANANA.\nBANANA\nTHIRD\n"
agy re-echoes the full prior transcript (newline-separated assistant replies, no
role markers) then appends the new reply. Extraction is PREFIX-STRIP off the
stored prior stdout.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[3]
    / "plugins" / "a2a_fleet" / "templates" / "agy_receiver.py"
)


@pytest.fixture(scope="module")
def agr():
    spec = importlib.util.spec_from_file_location("agy_receiver_under_test", TEMPLATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _isolate_receiver_runtime_paths(agr, tmp_path, monkeypatch):
    runtime = tmp_path / "receiver-runtime"
    runtime.mkdir()
    for attr, name in (
        ("CONFIG_PATH", "agy_receiver.json"),
        ("INBOX_PATH", "a2a-agy-inbox.jsonl"),
        ("INBOX_OFFSET_PATH", "a2a-agy-inbox.offset"),
        ("TRANSCRIPT_PATH", "a2a-agy-transcript.jsonl"),
        ("PID_PATH", "agy_receiver.pid"),
        ("TOKEN_PATH", ".agy-token"),
        ("SESSION_MAP_PATH", "a2a-agy-sessions.json"),
    ):
        monkeypatch.setattr(agr, attr, runtime / name, raising=False)


def _base_cfg(agr, repo: Path) -> dict:
    cfg = dict(agr.DEFAULTS)
    cfg["repo_path"] = str(repo)
    cfg["role_prompt"] = "ROLE-PROMPT-MARKER"
    return cfg


# Realistic captured samples (agy v1.0.4).
TURN1_STDOUT = "remembered the word BANANA.\n"
TURN2_STDOUT = "remembered the word BANANA.\nBANANA\n"
TURN3_STDOUT = "remembered the word BANANA.\nBANANA\nTHIRD\n"


def test_agy_receiver_template_compiles() -> None:
    import py_compile
    py_compile.compile(str(TEMPLATE_PATH), doraise=True)


def test_isolated_runtime_filenames(agr) -> None:
    assert agr.CONFIG_PATH.name == "agy_receiver.json"
    assert agr.INBOX_PATH.name == "a2a-agy-inbox.jsonl"
    assert agr.INBOX_OFFSET_PATH.name == "a2a-agy-inbox.offset"
    assert agr.TRANSCRIPT_PATH.name == "a2a-agy-transcript.jsonl"
    assert agr.PID_PATH.name == "agy_receiver.pid"
    assert agr.TOKEN_PATH.name == ".agy-token"
    assert agr.SESSION_MAP_PATH.name == "a2a-agy-sessions.json"
    assert agr.DEFAULTS["bind_port"] == 9313


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------

def test_build_command_first_turn_no_conversation(agr, tmp_path) -> None:
    cfg = _base_cfg(agr, tmp_path)
    first = agr.build_agy_command("do it", cfg, conversation_id=None)
    assert first[0] == "agy"
    assert "--conversation" not in first
    assert "--print" in first
    assert "--dangerously-skip-permissions" in first
    # No --model flag exists for agy.
    assert "--model" not in first
    # No --sandbox by default.
    assert "--sandbox" not in first
    # No --continue (cwd-global, unsafe).
    assert "--continue" not in first


def test_build_command_resume_turn(agr, tmp_path) -> None:
    cfg = _base_cfg(agr, tmp_path)
    resume = agr.build_agy_command("next step", cfg, conversation_id="uuid-xyz")
    assert resume[0] == "agy"
    assert "--conversation" in resume
    assert resume[resume.index("--conversation") + 1] == "uuid-xyz"
    assert "--print" in resume
    assert "--dangerously-skip-permissions" in resume
    assert "--model" not in resume


def test_build_command_sandbox_toggle(agr, tmp_path) -> None:
    cfg = _base_cfg(agr, tmp_path)
    cfg["agy_sandbox"] = True
    cmd = agr.build_agy_command("hi", cfg, conversation_id=None)
    assert "--sandbox" in cmd
    # --sandbox is a boolean toggle: the token after it must NOT be a value.
    idx = cmd.index("--sandbox")
    # last token or followed by another flag — never a bare value
    assert idx == len(cmd) - 1 or cmd[idx + 1].startswith("-")


def test_extra_flags_forbidden_session_selectors_stripped(agr, tmp_path) -> None:
    cfg = _base_cfg(agr, tmp_path)
    cfg["agy_extra_flags"] = ["--continue", "--conversation", "deadbeef", "--print", "x", "--foo", "bar"]
    cmd = agr.build_agy_command("work", cfg, conversation_id="real-uuid")
    # forbidden session/print selectors removed
    assert cmd.count("--continue") == 0
    assert cmd.count("--print") == 1  # only the one we always set
    # the explicit --conversation we set survives; the injected one+value are stripped
    assert cmd.count("--conversation") == 1
    assert "deadbeef" not in cmd
    # allowed extra survives
    assert "--foo" in cmd
    assert "bar" in cmd


# ---------------------------------------------------------------------------
# Reply extraction (plain text, prefix-strip)
# ---------------------------------------------------------------------------

def test_extract_reply_first_turn_clean(agr) -> None:
    # First turn: prior_stdout is None -> whole stdout stripped.
    assert agr.extract_reply(TURN1_STDOUT, None) == "remembered the word BANANA."


def test_extract_reply_resume_prefix_strip(agr) -> None:
    # Resume turn 2: prior is turn1 stdout; new reply tail is "BANANA".
    assert agr.extract_reply(TURN2_STDOUT, TURN1_STDOUT) == "BANANA"


def test_extract_reply_resume_third_turn(agr) -> None:
    # Resume turn 3: prior is turn2 stdout; new reply tail is "THIRD".
    assert agr.extract_reply(TURN3_STDOUT, TURN2_STDOUT) == "THIRD"


def test_extract_reply_prefix_drift_returns_full_text(agr) -> None:
    # D2 regression: when the stored prefix does NOT match (e.g. restart lost
    # prior_stdout), return the FULL stdout, not just the last line. Dropping
    # earlier lines of a genuine multi-line reply silently loses content;
    # over-returning the re-echoed transcript is visible and recoverable.
    out = "para1\npara2\nFINAL\n"
    assert agr.extract_reply(out, "WRONG PRIOR") == "para1\npara2\nFINAL"


def test_extract_reply_empty_returns_none(agr) -> None:
    assert agr.extract_reply("", None) is None
    assert agr.extract_reply("\n\n", None) is None


def test_extract_reply_resume_only_whitespace_appended_returns_none(agr) -> None:
    # Resume where nothing new was appended beyond the prior transcript.
    assert agr.extract_reply(TURN1_STDOUT, TURN1_STDOUT) is None


# ---------------------------------------------------------------------------
# Session-not-found detection + warning stripping
# ---------------------------------------------------------------------------

DEAD_WARNING = (
    'Warning: conversation "00000000-0000-0000-0000-000000000000" not found.\n'
    "Hello! I am ready to pair program with you.\n"
)


def test_is_session_not_found_detects_warning(agr) -> None:
    assert agr.is_session_not_found(DEAD_WARNING) is True


def test_is_session_not_found_false_on_normal_reply(agr) -> None:
    assert agr.is_session_not_found(TURN1_STDOUT) is False
    assert agr.is_session_not_found("") is False


def test_strip_not_found_warning(agr) -> None:
    cleaned = agr.strip_not_found_warning(DEAD_WARNING)
    assert "Warning: conversation" not in cleaned
    assert cleaned == "Hello! I am ready to pair program with you."


def test_strip_not_found_warning_noop_when_absent(agr) -> None:
    assert agr.strip_not_found_warning("just a reply\n") == "just a reply"


# ---------------------------------------------------------------------------
# Conversation-id discovery from last_conversations.json
# ---------------------------------------------------------------------------

def test_discover_conversation_id_from_fake_file(agr, tmp_path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    fake = tmp_path / "last_conversations.json"
    fake.write_text(json.dumps({
        "/some/other/path": "other-uuid",
        str(repo): "the-real-uuid",
    }))
    assert agr.discover_conversation_id(repo, fake) == "the-real-uuid"


def test_discover_conversation_id_missing_file(agr, tmp_path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    assert agr.discover_conversation_id(repo, tmp_path / "nope.json") is None


def test_discover_conversation_id_repo_absent_returns_none(agr, tmp_path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    fake = tmp_path / "last_conversations.json"
    fake.write_text(json.dumps({"/elsewhere": "u"}))
    assert agr.discover_conversation_id(repo, fake) is None


# ---------------------------------------------------------------------------
# Session map round-trip (conversation_id + last_stdout)
# ---------------------------------------------------------------------------

def test_session_map_round_trip(agr) -> None:
    path = agr.SESSION_MAP_PATH
    assert agr.get_conversation_id_for_context("ctx-1", path) is None
    agr.store_session_for_context("ctx-1", "uuid-abc", TURN1_STDOUT, path)
    assert agr.get_conversation_id_for_context("ctx-1", path) == "uuid-abc"
    entry = agr.get_session_entry("ctx-1", path)
    assert entry["conversation_id"] == "uuid-abc"
    assert entry["last_stdout"] == TURN1_STDOUT
    raw = json.loads(path.read_text())
    assert raw["ctx-1"]["conversation_id"] == "uuid-abc"


def test_clear_session_for_context(agr) -> None:
    path = agr.SESSION_MAP_PATH
    agr.store_session_for_context("ctx-c", "uuid-c", "out", path)
    assert agr.get_conversation_id_for_context("ctx-c", path) == "uuid-c"
    agr.clear_session_for_context("ctx-c", path)
    assert agr.get_conversation_id_for_context("ctx-c", path) is None


# ---------------------------------------------------------------------------
# run_agy_turn: first-turn captures uuid (via discovery), resume strips prefix
# ---------------------------------------------------------------------------

def test_run_agy_turn_first_then_resume(agr, tmp_path, monkeypatch) -> None:
    cfg = _base_cfg(agr, tmp_path)
    calls = []

    # Stub conversation-id discovery (first turn mints "uuid-first").
    monkeypatch.setattr(agr, "discover_conversation_id", lambda repo_path, *a, **k: "uuid-first")

    def fake_runner(cmd, cwd, timeout):
        calls.append(cmd)
        if "--conversation" in cmd:
            # Resume turn: agy re-echoes prior transcript + new reply.
            return (TURN2_STDOUT, 0, "")
        # First turn: just the reply.
        return (TURN1_STDOUT, 0, "")

    first = agr.run_agy_turn("remember BANANA", "ctx-1", cfg, runner=fake_runner)
    second = agr.run_agy_turn("what word?", "ctx-1", cfg, runner=fake_runner)

    assert first == "remembered the word BANANA."
    assert second == "BANANA"
    # First call had no --conversation; second resumed with the stored uuid.
    assert "--conversation" not in calls[0]
    assert "--conversation" in calls[1]
    assert calls[1][calls[1].index("--conversation") + 1] == "uuid-first"
    # Stored uuid persisted.
    assert agr.get_conversation_id_for_context("ctx-1", agr.SESSION_MAP_PATH) == "uuid-first"


def test_run_agy_turn_first_turn_continuity_disabled_when_no_uuid(agr, tmp_path, monkeypatch) -> None:
    """If discovery yields no uuid on a first turn, the reply still returns and no
    session entry is persisted (continuity disabled for that turn)."""
    cfg = _base_cfg(agr, tmp_path)
    monkeypatch.setattr(agr, "discover_conversation_id", lambda repo_path, *a, **k: None)
    reply = agr.run_agy_turn("hi", "ctx-x", cfg, runner=lambda c, w, t: (TURN1_STDOUT, 0, ""))
    assert reply == "remembered the word BANANA."
    assert agr.get_conversation_id_for_context("ctx-x", agr.SESSION_MAP_PATH) is None


# ---------------------------------------------------------------------------
# Remint on dead conversation + stale-id clear
# ---------------------------------------------------------------------------

def test_run_agy_turn_remints_on_dead_conversation(agr, tmp_path, monkeypatch) -> None:
    """A resume against a dead uuid prints the not-found warning then runs fresh.

    The stale uuid is cleared, the warning is stripped from the reply, and the
    NEW uuid agy minted is captured + persisted.
    """
    cfg = _base_cfg(agr, tmp_path)
    path = agr.SESSION_MAP_PATH
    # Seed a dead uuid + a prior transcript.
    agr.store_session_for_context("ctx-dead", "uuid-dead", "old transcript\n", path)

    monkeypatch.setattr(agr, "discover_conversation_id", lambda repo_path, *a, **k: "uuid-new")

    def fake_runner(cmd, cwd, timeout):
        # The resume hit a dead uuid -> agy emits the warning then a fresh reply.
        return (DEAD_WARNING, 0, "")

    reply = agr.run_agy_turn("redo", "ctx-dead", cfg, runner=fake_runner)
    assert reply == "Hello! I am ready to pair program with you."
    # New uuid captured (not the dead one).
    assert agr.get_conversation_id_for_context("ctx-dead", path) == "uuid-new"


# ---------------------------------------------------------------------------
# D1 regression: first-turn uuid discovery is serialized by _FIRST_TURN_LOCK
# (cwd-keyed last_conversations.json is shared by all contextIds in this
# one-process-per-repo receiver, so concurrent first turns would cross-capture).
# ---------------------------------------------------------------------------

def test_run_agy_turn_first_turn_holds_first_turn_lock(agr, tmp_path, monkeypatch) -> None:
    """On a FIRST turn the discover+persist critical section runs while
    _FIRST_TURN_LOCK is held; a RESUME turn does NOT hold it (and does not even
    invoke discovery). Made to genuinely fail if the lock is removed."""
    cfg = _base_cfg(agr, tmp_path)
    observed = {}

    def recording_discover(repo_path, *a, **k):
        # Record whether the global first-turn lock is held when discovery runs.
        observed["locked_at_discovery"] = agr._FIRST_TURN_LOCK.locked()
        observed["discover_called"] = observed.get("discover_called", 0) + 1
        return "uuid-first"

    monkeypatch.setattr(agr, "discover_conversation_id", recording_discover)

    def fake_runner(cmd, cwd, timeout):
        if "--conversation" in cmd:
            return (TURN2_STDOUT, 0, "")
        return (TURN1_STDOUT, 0, "")

    # First turn: discovery must run with the lock held.
    agr.run_agy_turn("remember BANANA", "ctx-lock", cfg, runner=fake_runner)
    assert observed["discover_called"] == 1
    assert observed["locked_at_discovery"] is True
    # Lock released after the turn (no leak).
    assert agr._FIRST_TURN_LOCK.locked() is False

    # Resume turn: stored uuid known -> discovery is NOT invoked and the lock is
    # not taken for the resume path.
    agr.run_agy_turn("what word?", "ctx-lock", cfg, runner=fake_runner)
    assert observed["discover_called"] == 1  # unchanged: no discovery on resume
    assert agr._FIRST_TURN_LOCK.locked() is False


def test_run_agy_turn_remint_clears_stale_id_when_no_new_uuid(agr, tmp_path, monkeypatch) -> None:
    """Remint path removes the dead uuid from the map BEFORE persisting.

    If discovery yields NO new uuid after the remint, the map must have NO entry
    for that contextId — the dead uuid must NOT be left/re-persisted.

    This test FAILS if the clear_session_for_context call is removed (the dead
    uuid would survive) and PASSES with the fix.
    """
    cfg = _base_cfg(agr, tmp_path)
    path = agr.SESSION_MAP_PATH
    agr.store_session_for_context("ctx-stale", "uuid-dead", "old\n", path)
    assert agr.get_conversation_id_for_context("ctx-stale", path) == "uuid-dead"

    # Discovery fails to find a new uuid after the remint.
    monkeypatch.setattr(agr, "discover_conversation_id", lambda repo_path, *a, **k: None)

    def fake_runner(cmd, cwd, timeout):
        return (DEAD_WARNING, 0, "")

    reply = agr.run_agy_turn("redo", "ctx-stale", cfg, runner=fake_runner)
    # The dead uuid must be gone — not left on disk after a failed remint discovery.
    stored = agr.get_conversation_id_for_context("ctx-stale", path)
    assert stored is None, (
        f"Expected no conversation_id after failed remint, but found {stored!r} — "
        "clear_session_for_context must be called before re-persist"
    )
    # The reply is still returned (warning stripped).
    assert reply == "Hello! I am ready to pair program with you."


def test_run_agy_turn_cli_not_found(agr, tmp_path) -> None:
    cfg = _base_cfg(agr, tmp_path)

    def boom(cmd, cwd, timeout):
        raise agr.AgyCLINotFound("agy")

    reply = agr.run_agy_turn("hi", "ctx", cfg, runner=boom)
    assert reply == "[error] agy CLI not found on PATH"


def test_run_agy_turn_timeout_surfaces_auth_hint(agr, tmp_path) -> None:
    import subprocess as sp
    cfg = _base_cfg(agr, tmp_path)

    def hang(cmd, cwd, timeout):
        raise sp.TimeoutExpired(cmd, timeout)

    reply = agr.run_agy_turn("hi", "ctx", cfg, runner=hang)
    assert reply.startswith("[error] agy turn timed out")
    assert "not authenticated" in reply


def test_looks_like_auth_failure(agr) -> None:
    assert agr.looks_like_auth_failure("Error: not authenticated", "") is True
    assert agr.looks_like_auth_failure("", "please sign in to continue") is True
    assert agr.looks_like_auth_failure("normal reply", "") is False


# ---------------------------------------------------------------------------
# Bearer auth on the agy receiver HTTP layer
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


def _make_agy_request(agr, cfg, token, *, headers, body=b"", path="/jsonrpc", method="POST"):
    """Drive a Handler instance without a real socket."""
    HandlerCls = agr.make_handler(cfg, token, None)

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

    return H()


def test_agy_receiver_bearer_auth_wrong_token_returns_401(agr, tmp_path) -> None:
    cfg = _base_cfg(agr, tmp_path)
    h = _make_agy_request(agr, cfg, "correct-token",
                          headers={"Authorization": "Bearer wrong-token",
                                   "Content-Length": "2"},
                          body=b"{}")
    h.do_POST()
    assert h._status == 401


def test_agy_receiver_bearer_auth_missing_token_returns_401(agr, tmp_path) -> None:
    cfg = _base_cfg(agr, tmp_path)
    h = _make_agy_request(agr, cfg, "correct-token",
                          headers={"Content-Length": "2"},
                          body=b"{}")
    h.do_POST()
    assert h._status == 401


def test_agy_receiver_bearer_auth_correct_token_is_accepted(agr, tmp_path) -> None:
    cfg = _base_cfg(agr, tmp_path)
    body = json.dumps({
        "jsonrpc": "2.0", "id": "1", "method": "SendMessage",
        "params": {"message": {"contextId": "ctx-x", "parts": [{"text": "hi"}]}},
    }).encode()
    h = _make_agy_request(agr, cfg, "correct-token",
                          headers={"Authorization": "Bearer correct-token",
                                   "Content-Length": str(len(body))},
                          body=body)
    h.do_POST()
    assert h._status != 401, "Correct token was rejected with 401"


def test_nested_context_id_is_accepted_and_threads(agr, tmp_path) -> None:
    cfg = _base_cfg(agr, tmp_path)
    body = json.dumps({
        "jsonrpc": "2.0", "id": "1", "method": "SendMessage",
        "params": {"message": {"contextId": "ctx-nested", "parts": [{"text": "hi"}]}},
    }).encode()
    h = _make_agy_request(agr, cfg, None,
                          headers={"Content-Length": str(len(body))},
                          body=body)
    h.do_POST()
    resp = json.loads(h.wfile.buf)
    assert "error" not in resp
    assert resp["result"]["message"]["contextId"] == "ctx-nested"


def test_root_level_context_id_is_rejected_with_32602(agr, tmp_path) -> None:
    cfg = _base_cfg(agr, tmp_path)
    body = json.dumps({
        "jsonrpc": "2.0", "id": "1", "method": "SendMessage",
        "params": {"contextId": "ctx-root", "message": {"parts": [{"text": "hi"}]}},
    }).encode()
    h = _make_agy_request(agr, cfg, None,
                          headers={"Content-Length": str(len(body))},
                          body=body)
    h.do_POST()
    resp = json.loads(h.wfile.buf)
    assert resp["error"]["code"] == -32602
    assert resp["error"]["message"] == (
        "contextId must be nested under params.message, not at params root (A2A spec)"
    )
    assert not agr.INBOX_PATH.exists() or "ctx-root" not in agr.INBOX_PATH.read_text()


def test_main_fails_closed_for_non_loopback_without_auth(agr, monkeypatch, tmp_path) -> None:
    cfg = _base_cfg(agr, tmp_path)
    cfg["bind_host"] = "0.0.0.0"
    monkeypatch.setattr(agr, "load_config", lambda config_path=None: cfg)
    monkeypatch.setattr(agr, "resolve_auth_token", lambda c: None)
    rc = agr.main()
    assert rc == 2, "Expected exit code 2 for non-loopback bind without auth"
