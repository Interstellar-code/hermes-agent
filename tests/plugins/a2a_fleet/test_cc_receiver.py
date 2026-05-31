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
