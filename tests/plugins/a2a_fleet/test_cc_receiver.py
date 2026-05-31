"""Unit tests for the standalone cc_receiver template (stdlib/pytest only).

The template lives under ``plugins/a2a_fleet/templates/cc_receiver.py`` and is a
standalone script (not part of the importable package). We load it via importlib
from its file path so we can exercise its pure functions without spawning a live
``claude`` CLI or hitting the network.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[3]
    / "plugins" / "a2a_fleet" / "templates" / "cc_receiver.py"
)


@pytest.fixture(scope="module")
def ccr():
    """Import the template module by path."""
    spec = importlib.util.spec_from_file_location("cc_receiver_under_test", TEMPLATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Session-id determinism
# ---------------------------------------------------------------------------

def test_session_id_deterministic_same_context(ccr):
    a = ccr.session_id_for_context("ctx-123")
    b = ccr.session_id_for_context("ctx-123")
    assert a == b
    # valid uuid string
    assert len(a) == 36 and a.count("-") == 4


def test_session_id_distinct_contexts_differ(ccr):
    assert ccr.session_id_for_context("ctx-a") != ccr.session_id_for_context("ctx-b")


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------

def _base_cfg(ccr, repo: Path) -> dict:
    cfg = dict(ccr.DEFAULTS)
    cfg["repo_path"] = str(repo)
    cfg["claude_model"] = "sonnet"
    cfg["role_prompt"] = "ROLE-PROMPT-MARKER"
    return cfg


def test_command_builder_core_flags(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    sid = ccr.session_id_for_context("ctx-1")
    cmd = ccr.build_claude_command("do it", sid, cfg, resume=False, mcp_config_path=None)
    assert cmd[0] == "claude"
    assert cmd[1] == "-p"
    assert "do it" in cmd
    assert "--output-format" in cmd and "stream-json" in cmd
    assert "--verbose" in cmd
    assert "--permission-mode" in cmd and "bypassPermissions" in cmd
    # critical: opt into repo settings/MCP in headless
    i = cmd.index("--setting-sources")
    assert cmd[i + 1] == "user,project,local"
    assert "--model" in cmd and "sonnet" in cmd
    assert "--append-system-prompt" in cmd and "ROLE-PROMPT-MARKER" in cmd
    assert "--bare" not in cmd  # NO --bare


def test_command_builder_first_turn_uses_session_id(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    sid = ccr.session_id_for_context("ctx-1")
    cmd = ccr.build_claude_command("x", sid, cfg, resume=False)
    assert "--session-id" in cmd
    assert cmd[cmd.index("--session-id") + 1] == sid
    assert "--resume" not in cmd


def test_command_builder_resume_turn_uses_resume(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    sid = ccr.session_id_for_context("ctx-1")
    cmd = ccr.build_claude_command("x", sid, cfg, resume=True)
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == sid
    assert "--session-id" not in cmd


def test_command_builder_omits_mcp_when_absent(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    sid = ccr.session_id_for_context("ctx-1")
    cmd = ccr.build_claude_command("x", sid, cfg, resume=False, mcp_config_path=None)
    assert "--mcp-config" not in cmd


def test_command_builder_includes_mcp_when_present(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{}")
    sid = ccr.session_id_for_context("ctx-1")
    cmd = ccr.build_claude_command("x", sid, cfg, resume=False, mcp_config_path=mcp)
    assert "--mcp-config" in cmd
    assert cmd[cmd.index("--mcp-config") + 1] == str(mcp)


def test_command_builder_appends_extra_flags(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    cfg["claude_extra_flags"] = ["--foo", "bar"]
    sid = ccr.session_id_for_context("ctx-1")
    cmd = ccr.build_claude_command("x", sid, cfg, resume=False)
    assert cmd[-2:] == ["--foo", "bar"]


def test_resolve_mcp_config(ccr, tmp_path):
    assert ccr.resolve_mcp_config(tmp_path) is None  # absent
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{ not json")
    assert ccr.resolve_mcp_config(tmp_path) is None  # malformed -> skip, no crash
    mcp.write_text('{"mcpServers": {}}')
    assert ccr.resolve_mcp_config(tmp_path) == mcp  # valid -> path


# ---------------------------------------------------------------------------
# Deterministic result parsing
# ---------------------------------------------------------------------------

def test_parse_picks_final_result_frame(ccr):
    out = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "thinking"}]}}),
        json.dumps({"type": "result", "subtype": "success", "result": "FIRST"}),
        json.dumps({"type": "result", "subtype": "success", "result": "FINAL"}),
    ])
    assert ccr.parse_claude_output(out) == "FINAL"


def test_parse_handles_error_frame(ccr):
    out = json.dumps({"type": "result", "is_error": True, "subtype": "error_max_turns",
                      "result": "hit limit"})
    parsed = ccr.parse_claude_output(out)
    assert parsed is not None and parsed.startswith("[error]")
    assert "hit limit" in parsed


def test_parse_error_subtype_without_is_error(ccr):
    out = json.dumps({"type": "result", "subtype": "error_during_execution", "result": ""})
    parsed = ccr.parse_claude_output(out)
    assert parsed is not None and parsed.startswith("[error]")


def test_parse_falls_back_to_assistant_text(ccr):
    # result frame present but empty result text -> fall back to assistant
    out = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ANSWER"}]}}),
        json.dumps({"type": "result", "subtype": "success", "result": ""}),
    ])
    assert ccr.parse_claude_output(out) == "ANSWER"


def test_parse_returns_none_when_nothing_usable(ccr):
    out = json.dumps({"type": "system", "subtype": "init"})
    assert ccr.parse_claude_output(out) is None


def test_parse_ignores_non_json_lines(ccr):
    out = "garbage line\n" + json.dumps({"type": "result", "subtype": "success", "result": "OK"})
    assert ccr.parse_claude_output(out) == "OK"


# ---------------------------------------------------------------------------
# Per-contextId lock — same context serializes, different contexts concurrent
# ---------------------------------------------------------------------------

def test_context_locks_same_context_serialize(ccr, tmp_path, monkeypatch):
    cfg = _base_cfg(ccr, tmp_path)
    cfg["hermes_url"] = "http://127.0.0.1:1/jsonrpc"
    # never actually POST
    monkeypatch.setattr(ccr, "post_reply", lambda *a, **k: True)

    overlap = {"max": 0, "active": 0}
    overlap_lock = threading.Lock()

    def fake_runner(cmd, cwd, timeout):
        with overlap_lock:
            overlap["active"] += 1
            overlap["max"] = max(overlap["max"], overlap["active"])
        time.sleep(0.15)
        with overlap_lock:
            overlap["active"] -= 1
        return (json.dumps({"type": "result", "subtype": "success", "result": "ok"}), 0)

    recv = ccr.Receiver(cfg, runner=fake_runner)

    threads = [
        threading.Thread(target=recv.process_message, args=("same-ctx", f"msg{i}"))
        for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlap["max"] == 1, "same-context turns must NOT overlap"


def test_context_locks_different_contexts_concurrent(ccr, tmp_path, monkeypatch):
    cfg = _base_cfg(ccr, tmp_path)
    cfg["hermes_url"] = "http://127.0.0.1:1/jsonrpc"
    monkeypatch.setattr(ccr, "post_reply", lambda *a, **k: True)

    overlap = {"max": 0, "active": 0}
    overlap_lock = threading.Lock()

    def fake_runner(cmd, cwd, timeout):
        with overlap_lock:
            overlap["active"] += 1
            overlap["max"] = max(overlap["max"], overlap["active"])
        time.sleep(0.15)
        with overlap_lock:
            overlap["active"] -= 1
        return (json.dumps({"type": "result", "subtype": "success", "result": "ok"}), 0)

    recv = ccr.Receiver(cfg, runner=fake_runner)

    threads = [
        threading.Thread(target=recv.process_message, args=(f"ctx-{i}", "msg"))
        for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlap["max"] >= 2, "different contexts must run concurrently"


# ---------------------------------------------------------------------------
# Config loading + defaults
# ---------------------------------------------------------------------------

def test_load_config_defaults_when_missing(ccr, tmp_path):
    cfg = ccr.load_config(tmp_path / "nope.json")
    assert cfg["bind_port"] == ccr.DEFAULTS["bind_port"]
    assert cfg["claude_model"] == ccr.DEFAULTS["claude_model"]
    assert cfg["claude_extra_flags"] == []


def test_load_config_merges_overrides(ccr, tmp_path):
    cfgfile = tmp_path / "a2a_receiver.json"
    cfgfile.write_text(json.dumps({
        "repo_path": "/some/repo",
        "bind_port": 9999,
        "claude_model": "opus",
        "claude_extra_flags": ["--add-dir", "/x"],
    }))
    cfg = ccr.load_config(cfgfile)
    assert cfg["repo_path"] == "/some/repo"
    assert cfg["bind_port"] == 9999
    assert cfg["claude_model"] == "opus"
    assert cfg["claude_extra_flags"] == ["--add-dir", "/x"]
    # untouched key keeps default
    assert cfg["hermes_url"] == ccr.DEFAULTS["hermes_url"]


def test_load_config_malformed_uses_defaults(ccr, tmp_path):
    cfgfile = tmp_path / "a2a_receiver.json"
    cfgfile.write_text("{ broken json")
    cfg = ccr.load_config(cfgfile)
    assert cfg["bind_port"] == ccr.DEFAULTS["bind_port"]


def test_load_config_role_file_overrides_role_prompt(ccr, tmp_path):
    role = tmp_path / "ROLE.md"
    role.write_text("  custom role text  ")
    cfgfile = tmp_path / "a2a_receiver.json"
    cfgfile.write_text(json.dumps({"role_file": str(role)}))
    cfg = ccr.load_config(cfgfile)
    assert cfg["role_prompt"] == "custom role text"


def test_resolve_auth_token(ccr, monkeypatch):
    assert ccr.resolve_auth_token({"auth_token_env": None}) is None
    monkeypatch.delenv("CC_TEST_TOKEN", raising=False)
    assert ccr.resolve_auth_token({"auth_token_env": "CC_TEST_TOKEN"}) is None
    monkeypatch.setenv("CC_TEST_TOKEN", "s3cr3t")
    assert ccr.resolve_auth_token({"auth_token_env": "CC_TEST_TOKEN"}) == "s3cr3t"


# ---------------------------------------------------------------------------
# Fix 1 — inbox offset persistence (no reprocess after restart)
# ---------------------------------------------------------------------------

def _ok_runner(*_a, **_k):
    return (json.dumps({"type": "result", "subtype": "success", "result": "ok"}), 0)


def test_offset_persists_and_skips_backlog_after_restart(ccr, tmp_path, monkeypatch):
    monkeypatch.setattr(ccr, "post_reply", lambda *a, **k: True)
    inbox = tmp_path / "a2a-inbox.jsonl"
    offset = tmp_path / "a2a-inbox.offset"
    inbox.write_text("\n".join([
        json.dumps({"from": "hermes", "contextId": "c1", "text": "one"}),
        json.dumps({"from": "hermes", "contextId": "c2", "text": "two"}),
    ]) + "\n")

    calls = []

    def runner(cmd, cwd, timeout):
        return _ok_runner()

    cfg = _base_cfg(ccr, tmp_path)
    cfg["hermes_url"] = "http://127.0.0.1:1/jsonrpc"
    recv = ccr.Receiver(cfg, runner=runner, inbox_path=inbox, offset_path=offset)
    # Spy on dispatch so we can count without spawning real work.
    monkeypatch.setattr(recv, "process_message",
                        lambda cid, text: calls.append((cid, text)))
    recv.poll_once()
    time.sleep(0.05)
    assert len(calls) == 2
    assert offset.read_text().strip() == "2"

    # Simulate restart: brand-new Receiver reads the persisted offset.
    calls2 = []
    recv2 = ccr.Receiver(cfg, runner=runner, inbox_path=inbox, offset_path=offset)
    monkeypatch.setattr(recv2, "process_message",
                        lambda cid, text: calls2.append((cid, text)))
    recv2.poll_once()
    time.sleep(0.05)
    assert calls2 == [], "restart must NOT reprocess historical backlog"

    # A NEW inbound line is picked up and the offset advances.
    with inbox.open("a") as f:
        f.write(json.dumps({"from": "hermes", "contextId": "c3", "text": "three"}) + "\n")
    recv2.poll_once()
    time.sleep(0.05)
    assert len(calls2) == 1 and calls2[0][0] == "c3"
    assert offset.read_text().strip() == "3"


# ---------------------------------------------------------------------------
# Fix 2 — bounded concurrency semaphore
# ---------------------------------------------------------------------------

def test_bounded_concurrency_caps_simultaneous_turns(ccr, tmp_path, monkeypatch):
    monkeypatch.setattr(ccr, "post_reply", lambda *a, **k: True)
    cfg = _base_cfg(ccr, tmp_path)
    cfg["hermes_url"] = "http://127.0.0.1:1/jsonrpc"
    cfg["max_concurrent_turns"] = 2

    overlap = {"max": 0, "active": 0}
    olock = threading.Lock()

    def fake_runner(cmd, cwd, timeout):
        with olock:
            overlap["active"] += 1
            overlap["max"] = max(overlap["max"], overlap["active"])
        time.sleep(0.15)
        with olock:
            overlap["active"] -= 1
        return _ok_runner()

    recv = ccr.Receiver(cfg, runner=fake_runner)
    # Distinct contexts -> would be unbounded without the semaphore.
    threads = [threading.Thread(target=recv.process_message, args=(f"ctx-{i}", "m"))
               for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlap["max"] <= 2, "concurrency must be capped at max_concurrent_turns"


def test_concurrency_cap_replies_busy_when_full(ccr, tmp_path, monkeypatch):
    posted = []
    monkeypatch.setattr(ccr, "post_reply", lambda url, cid, text: posted.append(text) or True)
    cfg = _base_cfg(ccr, tmp_path)
    cfg["hermes_url"] = "http://127.0.0.1:1/jsonrpc"
    cfg["max_concurrent_turns"] = 1
    cfg["context_lock_wait_s"] = 0.05  # short bounded wait -> busy

    started = threading.Event()
    release = threading.Event()

    def blocking_runner(cmd, cwd, timeout):
        started.set()
        release.wait(2.0)
        return _ok_runner()

    recv = ccr.Receiver(cfg, runner=blocking_runner)
    t1 = threading.Thread(target=recv.process_message, args=("ctx-a", "m"))
    t1.start()
    assert started.wait(2.0)
    # Second distinct context can't get a slot -> [busy] reply.
    reply = recv.process_message("ctx-b", "m")
    assert reply == "[busy] max concurrent turns reached, retry"
    release.set()
    t1.join()


# ---------------------------------------------------------------------------
# Fix 3 — registry eviction never evicts a held lock
# ---------------------------------------------------------------------------

def test_context_locks_eviction_bounds_registry(ccr):
    locks = ccr.ContextLocks(max_entries=3)
    for i in range(10):
        locks.get(f"ctx-{i}")
    assert locks.size() <= 3


def test_context_locks_never_evicts_held_lock(ccr):
    locks = ccr.ContextLocks(max_entries=2)
    held = locks.get("held")
    assert held.acquire(blocking=False)
    try:
        # Add many more contexts; the held lock must survive eviction.
        for i in range(20):
            locks.get(f"other-{i}")
        # Re-requesting "held" must return the SAME lock object (not evicted).
        assert locks.get("held") is held
        assert held.locked()
    finally:
        held.release()


def test_seen_contexts_bounded(ccr):
    seen = ccr.SeenContexts(max_entries=3)
    for i in range(10):
        seen.mark(f"c{i}")
    assert seen.size() <= 3
    # Most recent retained.
    assert seen.has("c9")


# ---------------------------------------------------------------------------
# Fix 5 — narrowed session-retry trigger
# ---------------------------------------------------------------------------

def test_no_retry_on_generic_nonzero_rc(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    seen = ccr.SeenContexts()
    calls = []

    def runner(cmd, cwd, timeout):
        calls.append(cmd)
        # Generic failure: rc!=0, no parseable frames, no session signal.
        return ("not json garbage", 1, "boom: something broke")

    reply = ccr.run_claude_turn("hi", "ctx-x", cfg, runner=runner, seen=seen)
    assert len(calls) == 1, "generic rc!=0 must NOT trigger a session-mode retry"
    assert reply is not None and reply.startswith("[error]")
    assert "boom" in reply  # stderr snippet surfaced


def test_retry_only_on_true_session_not_found(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    seen = ccr.SeenContexts()
    seen.mark("ctx-x")  # so first attempt is resume
    calls = []

    def runner(cmd, cwd, timeout):
        calls.append(list(cmd))
        if "--resume" in cmd:
            return ("", 1, "Error: No conversation found with session id abc")
        return (json.dumps({"type": "result", "subtype": "success", "result": "RECOVERED"}), 0, "")

    reply = ccr.run_claude_turn("hi", "ctx-x", cfg, runner=runner, seen=seen)
    assert len(calls) == 2, "true session-not-found must retry the other mode once"
    assert reply == "RECOVERED"


def test_claude_not_found_is_distinct_fatal(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    seen = ccr.SeenContexts()

    def runner(cmd, cwd, timeout):
        raise FileNotFoundError("claude")

    reply = ccr.run_claude_turn("hi", "ctx-x", cfg, runner=runner, seen=seen)
    assert reply == "[error] claude CLI not found on PATH"


# ---------------------------------------------------------------------------
# Fix 6 — fail-closed bind (non-loopback + no token)
# ---------------------------------------------------------------------------

def test_is_loopback_bind(ccr):
    assert ccr.is_loopback_bind("127.0.0.1")
    assert ccr.is_loopback_bind("::1")
    assert ccr.is_loopback_bind("localhost")
    assert not ccr.is_loopback_bind("0.0.0.0")
    assert not ccr.is_loopback_bind("10.0.0.5")


def test_main_refuses_nonloopback_without_token(ccr, tmp_path, monkeypatch):
    cfg = _base_cfg(ccr, tmp_path)
    cfg["bind_host"] = "0.0.0.0"
    cfg["auth_token_env"] = None
    monkeypatch.setattr(ccr, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(ccr, "resolve_auth_token", lambda *a, **k: None)
    # If it (wrongly) proceeds, these would be touched; ensure they aren't needed.
    rc = ccr.main()
    assert rc == 2, "must refuse to start (non-zero) on non-loopback bind w/o token"


# ---------------------------------------------------------------------------
# Fixes 7 & 9 — HTTP handler: malformed bearer 401, Content-Length cap
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


def _make_request(ccr, cfg, token, *, headers, body=b"", path="/jsonrpc", method="POST"):
    """Drive a Handler instance without a real socket."""
    HandlerCls = ccr.make_handler(cfg, token, None)

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


def test_malformed_bearer_header_returns_401(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    # "bearer" prefix matched but no token after it -> must be 401, not 500.
    h = _make_request(ccr, cfg, "expected-token",
                      headers={"Authorization": "bearer "}, body=b"{}")
    h.do_POST()
    assert h._status == 401
    # Empty-token variant ("bearer    ").
    h2 = _make_request(ccr, cfg, "expected-token",
                       headers={"Authorization": "bearer    "}, body=b"{}")
    h2.do_POST()
    assert h2._status == 401


def test_content_length_too_large_returns_413(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    big = str(ccr.MAX_BODY_BYTES + 1)
    h = _make_request(ccr, cfg, None,
                      headers={"Content-Length": big}, body=b"{}")
    h.do_POST()
    assert h._status == 413


def test_content_length_malformed_returns_error(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    h = _make_request(ccr, cfg, None,
                      headers={"Content-Length": "notanint"}, body=b"{}")
    h.do_POST()
    # JSON-RPC error envelope is sent at HTTP 200 with code -32600.
    payload = json.loads(h.wfile.buf.decode())
    assert payload["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# Fix 11 — idle-timeout self-teardown
# ---------------------------------------------------------------------------

def test_idle_monitor_triggers_teardown(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    cfg["idle_timeout_s"] = 0.1
    fired = {"n": 0}
    recv = ccr.Receiver(cfg, on_idle_shutdown=lambda: fired.__setitem__("n", fired["n"] + 1))
    # Not idle yet.
    assert recv.idle_monitor_once() is False
    time.sleep(0.15)
    assert recv.idle_monitor_once() is True
    assert fired["n"] == 1
    assert recv._stop.is_set()


def test_idle_monitor_disabled_when_zero(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    cfg["idle_timeout_s"] = 0
    recv = ccr.Receiver(cfg)
    time.sleep(0.05)
    assert recv.idle_monitor_once() is False


def test_note_inbound_resets_idle_clock(ccr, tmp_path):
    cfg = _base_cfg(ccr, tmp_path)
    cfg["idle_timeout_s"] = 0.2
    recv = ccr.Receiver(cfg)
    time.sleep(0.15)
    recv.note_inbound()  # reset
    assert recv.idle_monitor_once() is False


# ---------------------------------------------------------------------------
# Fix 12 — anonymous contextId mints a fresh uuid4 (no shared sentinel)
# ---------------------------------------------------------------------------

def test_anon_context_mints_unique_uuid(ccr, tmp_path, monkeypatch):
    monkeypatch.setattr(ccr, "post_reply", lambda *a, **k: True)
    inbox = tmp_path / "a2a-inbox.jsonl"
    offset = tmp_path / "a2a-inbox.offset"
    inbox.write_text("\n".join([
        json.dumps({"from": "hermes", "text": "a"}),   # no contextId
        json.dumps({"from": "hermes", "text": "b"}),   # no contextId
    ]) + "\n")
    seen_ctx = []
    cfg = _base_cfg(ccr, tmp_path)
    recv = ccr.Receiver(cfg, runner=_ok_runner, inbox_path=inbox, offset_path=offset)
    monkeypatch.setattr(recv, "process_message",
                        lambda cid, text: seen_ctx.append(cid))
    recv.poll_once()
    time.sleep(0.05)
    assert len(seen_ctx) == 2
    assert seen_ctx[0] != seen_ctx[1], "anon contexts must not share a sentinel"
    assert all(c.startswith("anon-") for c in seen_ctx)


# ---------------------------------------------------------------------------
# Fix 8 — concurrent inbox appends do not tear lines
# ---------------------------------------------------------------------------

def test_concurrent_inbox_appends_no_torn_lines(ccr, tmp_path, monkeypatch):
    inbox = tmp_path / "a2a-inbox.jsonl"
    inbox.touch()
    monkeypatch.setattr(ccr, "INBOX_PATH", inbox)

    def writer(i):
        ccr._append_jsonl(inbox, {"from": "hermes", "contextId": f"c{i}",
                                  "text": "x" * 500}, ccr._INBOX_LOCK)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = [l for l in inbox.read_text().splitlines() if l.strip()]
    assert len(lines) == 40
    for l in lines:
        json.loads(l)  # every line must be valid JSON (not torn)
