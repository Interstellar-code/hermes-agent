"""Capability-parity guards for the opencode / codex / agy receiver templates.

These lock in the fixes that bring the three non-Claude executors up to the
claude_code parity bar — real tool/file/gh access driven non-interactively:

* codex (#97)  — prompt is a positional arg AND the runner closes stdin
  (stdin=DEVNULL); codex-cli >= 0.136 otherwise blocks "Reading additional input
  from stdin..." and exits rc=1.
* opencode (#99) — runs under the full-tool ``build`` agent and the spawned
  process gets an augmented PATH so its bash/tool calls find gh/git.
* agy (#100) — ``--add-dir <repo>`` grants workspace access and ``--print-timeout``
  is raised off agy's 5m default (which produced plan-only/no-result turns); the
  receiver's own subprocess backstop is strictly longer so agy self-exits first.

A real end-to-end "reply PONG with tools" smoke per mode is gated behind
A2A_LIVE_SMOKE=1 (and CLI availability) so normal/CI runs skip it.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_TEMPLATES = Path(__file__).resolve().parents[3] / "plugins" / "a2a_fleet" / "templates"


def _load(name: str):
    path = _TEMPLATES / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ocr():
    return _load("oc_receiver")


@pytest.fixture(scope="module")
def cxr():
    return _load("codex_receiver")


@pytest.fixture(scope="module")
def agr():
    return _load("agy_receiver")


def _cfg(mod, repo: Path) -> dict:
    cfg = dict(mod.DEFAULTS)
    cfg["repo_path"] = str(repo)
    return cfg


class _FakePopen:
    """Captures Popen kwargs; emulates a clean, instant run."""

    last_kwargs: dict = {}

    def __init__(self, *args, **kwargs):
        _FakePopen.last_kwargs = kwargs
        self.returncode = 0

    def communicate(self, timeout=None):
        return ("PONG", "")

    def kill(self):
        pass


# --- _tool_env (shared PATH augmentation) ----------------------------------


@pytest.mark.parametrize("modname", ["oc_receiver", "codex_receiver", "agy_receiver"])
def test_tool_env_appends_paths_without_shadowing(modname, monkeypatch):
    mod = _load(modname)
    monkeypatch.setenv("PATH", "/already/here")
    env = mod._tool_env()
    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == "/already/here", "existing PATH must not be shadowed"
    assert "/usr/bin" in parts, "common tool dir should be appended"
    # idempotent: existing entries are not duplicated
    assert parts.count("/usr/bin") == 1


def test_agy_tool_env_keeps_latex_disable(agr, monkeypatch):
    monkeypatch.setenv("PATH", "/x")
    assert agr._tool_env().get("AGY_CLI_DISABLE_LATEX") == "1"


# --- codex #97 -------------------------------------------------------------


def test_codex_prompt_is_positional_not_stdin(cxr, tmp_path):
    cmd = cxr.build_codex_command("do the thing", _cfg(cxr, tmp_path), thread_id=None)
    # The prompt (role-wrapped) is a single positional arg right after `exec`,
    # never piped via stdin.
    assert cmd[:2] == ["codex", "exec"]
    assert "do the thing" in cmd[2], "prompt must be passed as a positional arg"
    assert "--json" in cmd


def test_codex_runner_closes_stdin_and_augments_path(cxr, tmp_path, monkeypatch):
    monkeypatch.setattr(cxr.subprocess, "Popen", _FakePopen)
    cxr._subprocess_runner(["codex", "--version"], str(tmp_path), 5.0)
    assert _FakePopen.last_kwargs.get("stdin") is subprocess.DEVNULL, (
        "codex runner must close stdin (issue #97) so 0.136 uses the positional prompt"
    )
    assert "PATH" in (_FakePopen.last_kwargs.get("env") or {})


# --- opencode #99 ----------------------------------------------------------


def test_opencode_default_uses_opencode_default_primary_agent(ocr, tmp_path):
    # Default opencode_agent=None -> no --agent flag; opencode picks its own
    # default primary agent (which has the full tool set). Forcing "build" is
    # wrong (it is a subagent in some installs). The real #99 fix is PATH.
    cmd = ocr.build_opencode_command("analyze repo", _cfg(ocr, tmp_path), session_id=None)
    assert "--agent" not in cmd
    assert "--dangerously-skip-permissions" in cmd


def test_opencode_explicit_agent_is_passed(ocr, tmp_path):
    cfg = _cfg(ocr, tmp_path)
    cfg["opencode_agent"] = "my-primary"
    cmd = ocr.build_opencode_command("x", cfg, session_id=None)
    assert cmd.count("--agent") == 1
    assert cmd[cmd.index("--agent") + 1] == "my-primary"


def test_opencode_agent_cannot_be_injected_via_extra_flags(ocr, tmp_path):
    cfg = _cfg(ocr, tmp_path)  # default agent None
    cfg["opencode_extra_flags"] = ["--agent", "plan"]
    cmd = ocr.build_opencode_command("x", cfg, session_id=None)
    # --agent from extra_flags is stripped; default omits it entirely.
    assert "--agent" not in cmd


def test_opencode_runner_augments_path(ocr, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr.subprocess, "Popen", _FakePopen)
    ocr._subprocess_runner(["opencode", "--version"], str(tmp_path), 5.0)
    assert "PATH" in (_FakePopen.last_kwargs.get("env") or {})


# --- agy #100 --------------------------------------------------------------


def test_agy_adds_workspace_dir_and_print_timeout(agr, tmp_path):
    cfg = _cfg(agr, tmp_path)
    cfg["agy_timeout_s"] = 720
    cmd = agr.build_agy_command("analyze repo", cfg, conversation_id=None)
    assert "--add-dir" in cmd
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path)
    assert "--print-timeout" in cmd
    assert cmd[cmd.index("--print-timeout") + 1] == "720s"


def test_agy_managed_flags_cannot_be_overridden_via_extra(agr, tmp_path):
    cfg = _cfg(agr, tmp_path)
    cfg["agy_extra_flags"] = ["--add-dir", "/evil", "--print-timeout", "1s"]
    cmd = agr.build_agy_command("x", cfg, conversation_id=None)
    assert cmd.count("--add-dir") == 1 and cmd[cmd.index("--add-dir") + 1] == str(tmp_path)
    assert cmd.count("--print-timeout") == 1 and "1s" not in cmd


def test_agy_runner_augments_path_and_keeps_latex(agr, tmp_path, monkeypatch):
    # Guards the HIGH bug: _subprocess_runner must launch with the AUGMENTED env
    # (PATH + AGY_CLI_DISABLE_LATEX), not raw os.environ — else gh/git are missing
    # under a launchd daemon and the #100 fix is dead on the real path.
    monkeypatch.setattr(agr.subprocess, "Popen", _FakePopen)
    agr._subprocess_runner(["agy", "--print", "hi"], str(tmp_path), 5.0)
    env = _FakePopen.last_kwargs.get("env") or {}
    assert env.get("AGY_CLI_DISABLE_LATEX") == "1"
    assert "/usr/bin" in env.get("PATH", "").split(os.pathsep)
    assert _FakePopen.last_kwargs.get("stdin") is subprocess.DEVNULL


def test_agy_print_timeout_never_truncates_to_zero(agr, tmp_path):
    # MEDIUM: a tiny/fractional budget must floor at 1s, not 0s.
    cfg = _cfg(agr, tmp_path)
    cfg["agy_timeout_s"] = 0.5
    cmd = agr.build_agy_command("x", cfg, conversation_id=None)
    assert cmd[cmd.index("--print-timeout") + 1] == "1s"


def test_agy_prefix_drift_detection(agr):
    # #108: a resume whose stdout is not the persisted prefix = drift.
    assert agr._prefix_drifted("TOTALLY NEW", "OLD PREFIX") is True
    assert agr._prefix_drifted("OLD PREFIX\nmore", "OLD PREFIX") is False   # exact prefix
    assert agr._prefix_drifted("OLD PREFIX\nmore", "OLD PREFIX\n") is False  # rstripped prefix
    assert agr._prefix_drifted("anything", None) is False                    # first turn never drifts
    assert agr._prefix_drifted("", "OLD") is True                            # empty resume = drift


def test_agy_store_persists_prefix_drifted_flag(agr, tmp_path, monkeypatch):
    path = tmp_path / "sessions.json"
    monkeypatch.setattr(agr, "SESSION_MAP_PATH", path, raising=False)
    agr.store_session_for_context("ctx-1", "uuid-1", "out", path, prefix_drifted=True)
    rec = agr.get_session_entry("ctx-1", path)
    assert rec["prefix_drifted"] is True
    assert "drifted_at" in rec
    # clean turn clears the flag, drops drifted_at
    agr.store_session_for_context("ctx-1", "uuid-1", "out2", path, prefix_drifted=False)
    rec = agr.get_session_entry("ctx-1", path)
    assert rec["prefix_drifted"] is False
    assert "drifted_at" not in rec


def test_agy_run_turn_flags_drift_on_restart_resume(agr, tmp_path, monkeypatch):
    # #108 reproduction: a resume turn lands at a receiver whose persisted
    # last_stdout no longer prefixes agy's cumulative output -> flag drift, and
    # the reply is the (visible) full output, not a silent empty.
    runtime = tmp_path / "rt"
    runtime.mkdir()
    path = runtime / "sessions.json"
    monkeypatch.setattr(agr, "SESSION_MAP_PATH", path, raising=False)
    monkeypatch.setattr(agr, "TRANSCRIPT_PATH", runtime / "t.jsonl", raising=False)
    monkeypatch.setattr(agr, "discover_conversation_id", lambda *a, **k: "uuid-1", raising=False)
    # Pre-seed a resume session whose stored prefix will NOT match the new output.
    agr.store_session_for_context("ctx-drift", "uuid-1", "STALE TURN-1 TRANSCRIPT", path)

    cfg = _cfg(agr, tmp_path)
    reply = agr.run_agy_turn(
        "next", "ctx-drift", cfg,
        runner=lambda c, w, t: ("COMPLETELY DIFFERENT CUMULATIVE OUTPUT", 0, ""),
    )
    assert reply == "COMPLETELY DIFFERENT CUMULATIVE OUTPUT"  # visible, not empty
    assert agr.get_session_entry("ctx-drift", path)["prefix_drifted"] is True


def test_agy_empty_output_returns_actionable_auth_error(agr, tmp_path, monkeypatch):
    # #105: agy v1.0.4 --print exits rc=0 with EMPTY stdout/stderr when not
    # signed in (silent, no marker). The turn must surface the actionable
    # "agy not authenticated — run agy interactively..." hint, NOT the opaque
    # "[no reply produced by agy]" fallback.
    runtime = tmp_path / "rt"
    runtime.mkdir()
    monkeypatch.setattr(agr, "SESSION_MAP_PATH", runtime / "s.json", raising=False)
    monkeypatch.setattr(agr, "TRANSCRIPT_PATH", runtime / "t.jsonl", raising=False)
    monkeypatch.setattr(agr, "discover_conversation_id", lambda *a, **k: None, raising=False)

    cfg = _cfg(agr, tmp_path)
    res = agr.run_agy_turn("hi", "ctx-empty", cfg, runner=lambda c, w, t: ("", 0, ""))
    assert res is not None
    assert "not authenticated" in res.lower(), f"expected actionable auth hint, got {res!r}"
    assert "no reply produced" not in res.lower()


def test_agy_receiver_backstop_outlives_print_timeout(agr, tmp_path, monkeypatch):
    """run_agy_turn must hand the runner a backstop = agy_timeout_s + grace, so
    agy reaches its own --print-timeout and self-exits before being killed."""
    runtime = tmp_path / "rt"
    runtime.mkdir()
    monkeypatch.setattr(agr, "SESSION_MAP_PATH", runtime / "a2a-agy-sessions.json", raising=False)
    monkeypatch.setattr(agr, "TRANSCRIPT_PATH", runtime / "a2a-agy-transcript.jsonl", raising=False)
    monkeypatch.setattr(agr, "discover_conversation_id", lambda *a, **k: "uuid-1", raising=False)

    cfg = _cfg(agr, tmp_path)
    cfg["agy_timeout_s"] = 100

    seen = {}

    def fake_runner(cmd, cwd, timeout):
        seen["timeout"] = timeout
        return ("PONG", 0, "")

    agr.run_agy_turn("hi", "ctx-1", cfg, runner=fake_runner)
    assert seen["timeout"] == pytest.approx(100 + agr.AGY_TIMEOUT_GRACE_S)


# --- gated live smoke (skipped unless A2A_LIVE_SMOKE=1) ---------------------

_LIVE = os.environ.get("A2A_LIVE_SMOKE") == "1"


@pytest.mark.skipif(not _LIVE, reason="set A2A_LIVE_SMOKE=1 to run live executor smoke")
@pytest.mark.parametrize(
    "modname,cli,builder,kw",
    [
        ("codex_receiver", "codex", "build_codex_command", {"thread_id": None}),
        ("oc_receiver", "opencode", "build_opencode_command", {"session_id": None}),
        ("agy_receiver", "agy", "build_agy_command", {"conversation_id": None}),
    ],
)
def test_live_executor_replies_pong(modname, cli, builder, kw, tmp_path):
    if shutil.which(cli) is None:
        pytest.skip(f"{cli} not on PATH")
    mod = _load(modname)
    cfg = _cfg(mod, tmp_path)
    cmd = getattr(mod, builder)("Reply with only the word PONG.", cfg, **kw)
    out, rc, err = mod._subprocess_runner(cmd, str(tmp_path), 120.0)
    assert rc == 0, f"{cli} rc={rc} err={err[:200]}"
    assert "PONG" in (out or ""), f"{cli} did not reply PONG: {out[:200]!r}"
