"""Unit tests for the standalone OpenCode receiver template.

The template lives under ``plugins/a2a_fleet/templates/oc_receiver.py`` and is a
standalone script (not part of the importable package). Load it by path so we
can exercise its helpers without spawning a real ``opencode`` CLI or network.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[3]
    / "plugins" / "a2a_fleet" / "templates" / "oc_receiver.py"
)


@pytest.fixture(scope="module")
def ocr():
    spec = importlib.util.spec_from_file_location("oc_receiver_under_test", TEMPLATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _isolate_receiver_runtime_paths(ocr, tmp_path, monkeypatch):
    runtime = tmp_path / "receiver-runtime"
    runtime.mkdir()
    for attr, name in (
        ("CONFIG_PATH", "oc_receiver.json"),
        ("INBOX_PATH", "a2a-oc-inbox.jsonl"),
        ("INBOX_OFFSET_PATH", "a2a-oc-inbox.offset"),
        ("TRANSCRIPT_PATH", "a2a-oc-transcript.jsonl"),
        ("PID_PATH", "oc_receiver.pid"),
        ("TOKEN_PATH", ".oc-token"),
        ("SESSION_MAP_PATH", "a2a-oc-sessions.json"),
    ):
        monkeypatch.setattr(ocr, attr, runtime / name, raising=False)


def _base_cfg(ocr, repo: Path) -> dict:
    cfg = dict(ocr.DEFAULTS)
    cfg["repo_path"] = str(repo)
    cfg["role_prompt"] = "ROLE-PROMPT-MARKER"
    cfg["opencode_model"] = "gpt-oss"
    return cfg


def test_oc_receiver_template_compiles() -> None:
    import py_compile

    py_compile.compile(str(TEMPLATE_PATH), doraise=True)


def test_isolated_runtime_filenames(ocr) -> None:
    assert ocr.CONFIG_PATH.name == "oc_receiver.json"
    assert ocr.INBOX_PATH.name == "a2a-oc-inbox.jsonl"
    assert ocr.INBOX_OFFSET_PATH.name == "a2a-oc-inbox.offset"
    assert ocr.TRANSCRIPT_PATH.name == "a2a-oc-transcript.jsonl"
    assert ocr.PID_PATH.name == "oc_receiver.pid"
    assert ocr.TOKEN_PATH.name == ".oc-token"
    assert ocr.SESSION_MAP_PATH.name == "a2a-oc-sessions.json"


def test_session_map_round_trip(ocr) -> None:
    path = ocr.SESSION_MAP_PATH
    assert ocr.get_session_id_for_context("ctx-1", path) is None
    ocr.store_session_id_for_context("ctx-1", "ses_abc", path)
    assert ocr.get_session_id_for_context("ctx-1", path) == "ses_abc"
    raw = json.loads(path.read_text())
    assert raw["ctx-1"]["session_id"] == "ses_abc"
    assert isinstance(raw["ctx-1"]["updated_at"], int)


def test_build_command_first_turn_and_continue(ocr, tmp_path) -> None:
    cfg = _base_cfg(ocr, tmp_path)

    first = ocr.build_opencode_command("do it", cfg, session_id=None)
    assert first[:2] == ["opencode", "run"]
    assert first[2].endswith("do it")
    assert "--session" not in first
    assert "--format" in first and "json" in first
    assert "--dangerously-skip-permissions" in first
    assert "--model" in first and "gpt-oss" in first

    cont = ocr.build_opencode_command("do it", cfg, session_id="ses_live")
    assert "--session" in cont
    assert cont[cont.index("--session") + 1] == "ses_live"


def test_parse_opencode_output_extracts_session_and_text(ocr) -> None:
    out = "\n".join([
        json.dumps({
            "type": "step_start",
            "sessionID": "ses_live",
            "part": {"type": "step-start", "sessionID": "ses_live"},
        }),
        json.dumps({
            "type": "text",
            "part": {"type": "text", "text": "hello "},
        }),
        json.dumps({
            "type": "text",
            "part": {"type": "text", "text": "world"},
        }),
        json.dumps({
            "type": "step_finish",
            "part": {"type": "step-finish", "reason": "stop"},
        }),
    ])
    session_id, reply = ocr.parse_opencode_output(out)
    assert session_id == "ses_live"
    assert reply == "hello world"


def test_run_opencode_turn_persists_then_reuses_session(ocr, tmp_path, monkeypatch) -> None:
    cfg = _base_cfg(ocr, tmp_path)
    calls = []
    stored = {}

    monkeypatch.setattr(ocr, "get_session_id_for_context", lambda context_id: stored.get(context_id))
    monkeypatch.setattr(
        ocr, "store_session_id_for_context",
        lambda context_id, session_id: stored.__setitem__(context_id, session_id),
    )

    def fake_runner(cmd, cwd, timeout):
        calls.append(cmd)
        if stored.get("ctx-1") is not None:
            return (
                "\n".join([
                    json.dumps({"type": "step_start", "sessionID": "ses_first", "part": {"type": "step-start"}}),
                    json.dumps({"type": "text", "part": {"type": "text", "text": "turn-2"}}),
                ]),
                0,
                "",
            )
        return (
            "\n".join([
                json.dumps({"type": "step_start", "sessionID": "ses_first", "part": {"type": "step-start"}}),
                json.dumps({"type": "text", "part": {"type": "text", "text": "turn-1"}}),
            ]),
            0,
            "",
        )

    first = ocr.run_opencode_turn("turn one", "ctx-1", cfg, runner=fake_runner)
    second = ocr.run_opencode_turn("turn two", "ctx-1", cfg, runner=fake_runner)

    assert first == "turn-1"
    assert second == "turn-2"
    assert stored["ctx-1"] == "ses_first"
    assert "--session" not in calls[0]
    assert "--session" in calls[1]
    assert calls[1][calls[1].index("--session") + 1] == "ses_first"


def test_run_opencode_turn_remints_when_stored_session_missing(ocr, tmp_path, monkeypatch) -> None:
    cfg = _base_cfg(ocr, tmp_path)
    calls = []
    stored = {"ctx-1": "ses_dead"}

    monkeypatch.setattr(ocr, "get_session_id_for_context", lambda context_id: stored.get(context_id))
    monkeypatch.setattr(
        ocr, "store_session_id_for_context",
        lambda context_id, session_id: stored.__setitem__(context_id, session_id),
    )

    def fake_runner(cmd, cwd, timeout):
        calls.append(cmd)
        if "--session" in cmd:
            return ("", 1, "Error: Session not found")
        return (
            "\n".join([
                json.dumps({"type": "step_start", "sessionID": "ses_new", "part": {"type": "step-start"}}),
                json.dumps({"type": "text", "part": {"type": "text", "text": "fresh"}}),
            ]),
            0,
            "",
        )

    reply = ocr.run_opencode_turn("redo", "ctx-1", cfg, runner=fake_runner)
    assert reply == "fresh"
    assert stored["ctx-1"] == "ses_new"
    assert "--session" in calls[0]
    assert "--session" not in calls[1]


def test_run_opencode_turn_remints_when_dead_session_in_reply(ocr, tmp_path, monkeypatch) -> None:
    """Remint fires when the session-not-found signal appears in parsed reply text, not stderr.

    This test FAILS before the H1 fix (_is_session_not_found only checked stderr)
    and PASSES after (it checks both reply and stderr).
    """
    cfg = _base_cfg(ocr, tmp_path)
    calls = []
    stored = {"ctx-2": "ses_dead"}

    monkeypatch.setattr(ocr, "get_session_id_for_context", lambda context_id: stored.get(context_id))
    monkeypatch.setattr(
        ocr, "store_session_id_for_context",
        lambda context_id, session_id: stored.__setitem__(context_id, session_id),
    )

    def fake_runner(cmd, cwd, timeout):
        calls.append(cmd)
        if "--session" in cmd:
            # Dead-session signal in reply text (stdout), NOT stderr
            return (
                "\n".join([
                    json.dumps({"type": "text", "part": {"type": "text", "text": "Error: session not found"}}),
                ]),
                0,
                "",
            )
        return (
            "\n".join([
                json.dumps({"type": "step_start", "sessionID": "ses_fresh", "part": {"type": "step-start"}}),
                json.dumps({"type": "text", "part": {"type": "text", "text": "reminted"}}),
            ]),
            0,
            "",
        )

    reply = ocr.run_opencode_turn("test", "ctx-2", cfg, runner=fake_runner)
    assert reply == "reminted", (
        f"Expected reminted reply, got {reply!r} — "
        "remint did not fire on reply-text session signal (H1 bug)"
    )
    assert stored["ctx-2"] == "ses_fresh"
    assert len(calls) == 2
    assert "--session" in calls[0]
    assert "--session" not in calls[1]


# ---------------------------------------------------------------------------
# Bearer auth on the oc receiver HTTP layer
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


def _make_oc_request(ocr, cfg, token, *, headers, body=b"", path="/jsonrpc", method="POST"):
    """Drive a Handler instance without a real socket."""
    HandlerCls = ocr.make_handler(cfg, token, None)

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


def test_oc_receiver_bearer_auth_wrong_token_returns_401(ocr, tmp_path) -> None:
    """POST /jsonrpc with wrong token must return 401."""
    cfg = _base_cfg(ocr, tmp_path)
    h = _make_oc_request(ocr, cfg, "correct-token",
                         headers={"Authorization": "Bearer wrong-token",
                                  "Content-Length": "2"},
                         body=b"{}")
    h.do_POST()
    assert h._status == 401


def test_oc_receiver_bearer_auth_missing_token_returns_401(ocr, tmp_path) -> None:
    """POST /jsonrpc with no Authorization header must return 401 when token is configured."""
    cfg = _base_cfg(ocr, tmp_path)
    h = _make_oc_request(ocr, cfg, "correct-token",
                         headers={"Content-Length": "2"},
                         body=b"{}")
    h.do_POST()
    assert h._status == 401


def test_oc_receiver_bearer_auth_correct_token_is_accepted(ocr, tmp_path) -> None:
    """POST /jsonrpc with the correct token must NOT return 401."""
    cfg = _base_cfg(ocr, tmp_path)
    body = json.dumps({
        "jsonrpc": "2.0", "id": "1", "method": "SendMessage",
        "params": {"message": {"contextId": "ctx-x", "parts": [{"text": "hi"}]}},
    }).encode()
    h = _make_oc_request(ocr, cfg, "correct-token",
                         headers={"Authorization": "Bearer correct-token",
                                  "Content-Length": str(len(body))},
                         body=body)
    h.do_POST()
    assert h._status != 401, f"Correct token was rejected with 401"


def test_nested_context_id_is_accepted_and_threads(ocr, tmp_path) -> None:
    cfg = _base_cfg(ocr, tmp_path)
    body = json.dumps({
        "jsonrpc": "2.0", "id": "1", "method": "SendMessage",
        "params": {"message": {"contextId": "ctx-nested", "parts": [{"text": "hi"}]}},
    }).encode()
    h = _make_oc_request(ocr, cfg, None,
                         headers={"Content-Length": str(len(body))},
                         body=body)
    h.do_POST()
    resp = json.loads(h.wfile.buf)
    assert "error" not in resp
    assert resp["result"]["message"]["contextId"] == "ctx-nested"


def test_root_level_context_id_is_rejected_with_32602(ocr, tmp_path) -> None:
    cfg = _base_cfg(ocr, tmp_path)
    body = json.dumps({
        "jsonrpc": "2.0", "id": "1", "method": "SendMessage",
        "params": {"contextId": "ctx-root", "message": {"parts": [{"text": "hi"}]}},
    }).encode()
    h = _make_oc_request(ocr, cfg, None,
                         headers={"Content-Length": str(len(body))},
                         body=body)
    h.do_POST()
    resp = json.loads(h.wfile.buf)
    assert resp["error"]["code"] == -32602
    assert resp["error"]["message"] == (
        "contextId must be nested under params.message, not at params root (A2A spec)"
    )
    assert not ocr.INBOX_PATH.exists() or "ctx-root" not in ocr.INBOX_PATH.read_text()


# ---------------------------------------------------------------------------
# Fix A (#82): clear stale session_id before remint
# ---------------------------------------------------------------------------

def test_remint_clears_stale_session_when_fresh_retry_yields_no_session(ocr, tmp_path, monkeypatch) -> None:
    """Stale session_id must be cleared even when the fresh remint produces no new session_id.

    Fails before Fix A (clear_session_id_for_context not called in remint path):
      stored["ctx-stale"] still equals "ses_dead" after the turn.
    Passes after Fix A:
      stored["ctx-stale"] is gone (cleared before fresh retry, not re-stored).
    """
    cfg = _base_cfg(ocr, tmp_path)
    # Use an in-memory dict to avoid the default-arg binding issue on the path parameter.
    stored: dict = {"ctx-stale": "ses_dead"}

    monkeypatch.setattr(ocr, "get_session_id_for_context", lambda ctx, path=None: stored.get(ctx))
    monkeypatch.setattr(
        ocr, "store_session_id_for_context",
        lambda ctx, sid, path=None: stored.__setitem__(ctx, sid),
    )
    monkeypatch.setattr(
        ocr, "clear_session_id_for_context",
        lambda ctx, path=None: stored.pop(ctx, None),
    )

    def fake_runner(cmd, cwd, timeout):
        if "--session" in cmd:
            return ("", 1, "Error: Session not found")
        # Fresh retry: return output with NO sessionID field -> no new session stored.
        return (
            '{"type": "text", "part": {"type": "text", "text": "ok"}}\n',
            0,
            "",
        )

    ocr.run_opencode_turn("hi", "ctx-stale", cfg, runner=fake_runner)
    assert "ctx-stale" not in stored, (
        "Stale session_id 'ses_dead' was NOT cleared after a failed remint — "
        "Fix A (clear_session_id_for_context) is missing from the remint path."
    )


# ---------------------------------------------------------------------------
# Fix B (#86): _sanitize_extra_flags for oc_receiver
# ---------------------------------------------------------------------------

def test_sanitize_extra_flags_strips_forbidden_oc_tokens(ocr, tmp_path) -> None:
    """opencode_extra_flags with forbidden tokens must be stripped; safe tokens kept."""
    cfg = _base_cfg(ocr, tmp_path)
    cfg["opencode_extra_flags"] = [
        "--session", "x",       # forbidden (two-token form)
        "--format", "json",     # forbidden — builder already emits --format json once
        "--keep-me", "v",       # safe — must survive
    ]
    cmd = ocr.build_opencode_command("prompt", cfg, session_id=None)
    # --session / x from extra_flags must be stripped entirely.
    assert "--session" not in cmd
    assert "x" not in cmd
    # --format appears exactly once (from the builder itself, not from extra_flags).
    assert cmd.count("--format") == 1
    # "json" appears exactly once (from the builder, not doubled by extra_flags).
    assert cmd.count("json") == 1
    # Safe flags survive.
    assert "--keep-me" in cmd
    assert "v" in cmd


def test_main_fails_closed_for_non_loopback_without_auth(ocr, monkeypatch, tmp_path) -> None:
    cfg = _base_cfg(ocr, tmp_path)
    cfg["bind_host"] = "0.0.0.0"
    monkeypatch.setattr(ocr, "load_config", lambda config_path=None: cfg)
    monkeypatch.setattr(ocr, "resolve_auth_token", lambda cfg: None)

    assert ocr.main() == 2
