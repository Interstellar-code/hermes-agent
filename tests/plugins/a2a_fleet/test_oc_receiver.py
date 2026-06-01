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
    session_id, reply, session_missing = ocr.parse_opencode_output(out)
    assert session_id == "ses_live"
    assert reply == "hello world"
    assert session_missing is False


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


def test_main_fails_closed_for_non_loopback_without_auth(ocr, monkeypatch, tmp_path) -> None:
    cfg = _base_cfg(ocr, tmp_path)
    cfg["bind_host"] = "0.0.0.0"
    monkeypatch.setattr(ocr, "load_config", lambda config_path=None: cfg)
    monkeypatch.setattr(ocr, "resolve_auth_token", lambda cfg: None)

    assert ocr.main() == 2
