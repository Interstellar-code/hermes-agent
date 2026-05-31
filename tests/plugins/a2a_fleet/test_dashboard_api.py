"""Tests for the a2a_fleet dashboard API (conversation feed).

Loads dashboard/plugin_api.py exactly how web_server._mount_plugin_api_routes
does — as a flat spec module — so the sys.path/package-import bootstrap is
exercised the same way it runs in production.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PLUGIN_API = (
    Path(__file__).resolve().parents[3]
    / "plugins" / "a2a_fleet" / "dashboard" / "plugin_api.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_a2a_fleet_test", _PLUGIN_API
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _write_transcript(repo: Path, rows):
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir(parents=True, exist_ok=True)
    (hermes_dir / "a2a-transcript.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


@pytest.fixture
def api(monkeypatch, tmp_path):
    """Module with _managed_repos patched to a temp repo carrying a transcript."""
    mod = _load_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_transcript(repo, [
        {"ts": "2026-05-31 18:35:31", "dir": "hermes->claude", "from": "hermes",
         "to": "claude-code", "contextId": "ctx-1", "text": "do the thing"},
        {"ts": "2026-05-31 18:35:31", "dir": "claude->hermes (ack)", "from": "claude-code",
         "to": "hermes", "contextId": "ctx-1", "text": "received [queued]"},
        {"ts": "2026-05-31 18:35:47", "dir": "claude->hermes", "from": "claude-code",
         "to": "hermes", "contextId": "ctx-1", "text": "done: result here"},
        {"ts": "2026-05-31 18:40:00", "dir": "hermes->claude", "from": "hermes",
         "to": "claude-code", "contextId": "ctx-2", "text": "second thread"},
        "}{ this line is corrupt and must be skipped",
    ])
    monkeypatch.setattr(mod, "_managed_repos", lambda: [("claude-code", str(repo))])
    return mod, repo


def test_routes_present():
    mod = _load_module()
    paths = {r.path for r in mod.router.routes}
    assert "/conversations" in paths
    assert "/conversations/{context_id:path}" in paths
    assert "/peers" in paths


def test_list_conversations_groups_by_context_newest_first(api):
    mod, _ = api
    res = _run(mod.list_conversations())
    assert res["count"] == 2
    # ctx-2 (18:40) is newer than ctx-1 (18:35) -> first.
    assert [c["contextId"] for c in res["conversations"]] == ["ctx-2", "ctx-1"]
    ctx1 = next(c for c in res["conversations"] if c["contextId"] == "ctx-1")
    assert ctx1["message_count"] == 3  # corrupt line skipped
    assert ctx1["peer"] == "claude-code"
    assert ctx1["last_dir"] == "claude->hermes"
    assert "done: result here" in ctx1["last_text"]


def test_get_conversation_returns_ordered_messages(api):
    mod, _ = api
    res = _run(mod.get_conversation("ctx-1"))
    assert res["contextId"] == "ctx-1"
    assert [m["dir"] for m in res["messages"]] == [
        "hermes->claude", "claude->hermes (ack)", "claude->hermes",
    ]
    # contextId is stripped from per-message payload (redundant under the bucket).
    assert "contextId" not in res["messages"][0]


def test_get_conversation_unknown_context_404(api):
    mod, _ = api
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(mod.get_conversation("nope"))
    assert ei.value.status_code == 404


def test_peers_reports_transcript_presence(api):
    mod, _ = api
    res = _run(mod.list_peers())
    assert res["count"] == 1
    p = res["peers"][0]
    assert p["name"] == "claude-code"
    assert p["transcript_exists"] is True
    assert p["message_count"] == 4  # 4 valid rows, corrupt skipped


def test_no_managed_peers_degrades_to_empty(monkeypatch):
    mod = _load_module()
    monkeypatch.setattr(mod, "_managed_repos", lambda: [])
    res = _run(mod.list_conversations())
    assert res == {"conversations": [], "count": 0}
