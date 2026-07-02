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
    monkeypatch.setattr(mod, "_managed_repos", lambda: [("claude-code", str(repo), "claude_code")])
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


@pytest.fixture
def multirepo(monkeypatch, tmp_path):
    """Two repos that reuse the SAME contextId — must NOT merge into one bucket."""
    mod = _load_module()
    repo_a = tmp_path / "repo-a"; repo_a.mkdir()
    repo_b = tmp_path / "repo-b"; repo_b.mkdir()
    _write_transcript(repo_a, [
        {"ts": "2026-05-31 10:00:00", "dir": "hermes->claude", "contextId": "handshake:sw", "text": "A-only"},
    ])
    _write_transcript(repo_b, [
        {"ts": "2026-05-31 11:00:00", "dir": "hermes->claude", "contextId": "handshake:sw", "text": "B-only"},
    ])
    monkeypatch.setattr(mod, "_managed_repos", lambda: [
        ("cc-a", str(repo_a), "claude_code"), ("cc-b", str(repo_b), "claude_code"),
    ])
    return mod, repo_a, repo_b


def test_same_contextid_across_repos_does_not_merge(multirepo):
    mod, _, _ = multirepo
    res = _run(mod.list_conversations())
    assert res["count"] == 2  # one bucket per (repo, contextId), not merged
    texts = {(c["peer"], c["last_text"]) for c in res["conversations"]}
    assert ("cc-a", "A-only") in texts
    assert ("cc-b", "B-only") in texts


def test_ambiguous_contextid_returns_409_with_candidates(multirepo):
    mod, _, _ = multirepo
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(mod.get_conversation("handshake:sw"))
    assert ei.value.status_code == 409
    cands = ei.value.detail["candidates"]
    assert {c["peer"] for c in cands} == {"cc-a", "cc-b"}


def test_disambiguate_by_peer(multirepo):
    mod, _, _ = multirepo
    res = _run(mod.get_conversation("handshake:sw", peer="cc-b"))
    assert res["peer"] == "cc-b"
    assert res["messages"][0]["text"] == "B-only"


# ---------------------------------------------------------------------------
# Profile-agnostic discovery: a global dashboard (default HERMES_HOME) must find
# managed peers in OTHER profiles' fleet.yaml, not just its own.
# ---------------------------------------------------------------------------

def _write_managed_fleet(path: Path, peer_name: str, repo: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "fleet:\n"
        "  agents:\n"
        f"    {peer_name}:\n"
        f"      url: http://127.0.0.1:9300\n"
        f"      managed: true\n"
        f"      mode: claude_code\n"
        f"      repo_path: {repo}\n",
        encoding="utf-8",
    )


def test_managed_repos_scans_all_profiles(monkeypatch, tmp_path):
    mod = _load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    # home's own fleet.yaml + two profile fleet.yamls, each with a managed peer
    _write_managed_fleet(tmp_path / "fleet.yaml", "cc-home", "/repos/home")
    _write_managed_fleet(tmp_path / "profiles" / "switch" / "fleet.yaml", "claude-code", "/repos/switch")
    _write_managed_fleet(tmp_path / "profiles" / "other" / "fleet.yaml", "claude-code", "/repos/other")

    repos = {repo: name for name, repo, _mode in mod._managed_repos()}
    assert set(repos) == {"/repos/home", "/repos/switch", "/repos/other"}


def test_managed_repos_dedupes_same_repo_across_profiles(monkeypatch, tmp_path):
    mod = _load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_managed_fleet(tmp_path / "profiles" / "p1" / "fleet.yaml", "claude-code", "/repos/shared")
    _write_managed_fleet(tmp_path / "profiles" / "p2" / "fleet.yaml", "claude-code", "/repos/shared")
    assert mod._managed_repos() == [("claude-code", "/repos/shared", "claude_code")]


def test_managed_repos_skips_non_managed_and_bad_files(monkeypatch, tmp_path):
    mod = _load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # non-managed peer (managed absent) — must be skipped
    (tmp_path / "fleet.yaml").write_text(
        "fleet:\n  agents:\n    plain:\n      url: http://x\n      repo_path: /repos/plain\n",
        encoding="utf-8",
    )
    # corrupt profile fleet.yaml — must be skipped, not raise
    bad = tmp_path / "profiles" / "broken" / "fleet.yaml"
    bad.parent.mkdir(parents=True)
    bad.write_text("}{ not yaml", encoding="utf-8")
    assert mod._managed_repos() == []


def test_managed_repos_keeps_multiple_modes_in_one_repo(monkeypatch, tmp_path):
    """Issue #95: a cc + codex peer in the SAME repo must both surface.

    Regression guard — dedup is keyed by (repo, mode), so a second mode in the
    same repo is NOT dropped the way a repo-only key dropped it.
    """
    mod = _load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    (tmp_path / "fleet.yaml").write_text(
        "fleet:\n"
        "  agents:\n"
        "    claude-code:\n"
        "      url: http://127.0.0.1:9300\n"
        "      managed: true\n"
        "      mode: claude_code\n"
        "      repo_path: /repos/shared\n"
        "    codex:\n"
        "      url: http://127.0.0.1:9320\n"
        "      managed: true\n"
        "      mode: codex\n"
        "      repo_path: /repos/shared\n",
        encoding="utf-8",
    )
    rows = mod._managed_repos()
    modes = {mode for _name, _repo, mode in rows}
    assert modes == {"claude_code", "codex"}, (
        f"both modes for the shared repo must appear; got {rows}"
    )
    assert all(repo == "/repos/shared" for _n, repo, _m in rows)


def test_managed_repos_dedupes_same_repo_AND_mode_across_profiles(monkeypatch, tmp_path):
    """(repo, mode) dedup still collapses the SAME mode for the SAME repo seen in
    two profiles — only the mode dimension is newly distinguishing."""
    mod = _load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_managed_fleet(tmp_path / "profiles" / "p1" / "fleet.yaml", "codex", "/repos/shared")
    _write_managed_fleet(tmp_path / "profiles" / "p2" / "fleet.yaml", "codex", "/repos/shared")
    # _write_managed_fleet hardcodes mode: claude_code, so both rows share the
    # same (repo, mode) -> one row, proving same-mode dedup is intact.
    assert mod._managed_repos() == [("codex", "/repos/shared", "claude_code")]


# ---------------------------------------------------------------------------
# Mode-aware tests: _managed_repos includes all 4 modes; transcript path is
# per-mode; mixed fleet surfaces all modes in list_conversations / list_peers.
# ---------------------------------------------------------------------------

def _write_mode_fleet(path: Path, peer_name: str, mode: str, repo: str):
    """Write a minimal fleet.yaml with one managed peer of the given mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "fleet:\n"
        "  agents:\n"
        f"    {peer_name}:\n"
        f"      url: http://127.0.0.1:9300\n"
        f"      managed: true\n"
        f"      mode: {mode}\n"
        f"      repo_path: {repo}\n",
        encoding="utf-8",
    )


def _write_mode_transcript(repo: Path, filename: str, rows):
    """Write rows as JSONL to repo/.hermes/<filename>."""
    hermes_dir = repo / ".hermes"
    hermes_dir.mkdir(parents=True, exist_ok=True)
    (hermes_dir / filename).write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_managed_repos_returns_all_four_modes(monkeypatch, tmp_path):
    """_managed_repos() must surface claude_code, opencode, codex, and agy peers."""
    mod = _load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    modes = {
        "claude_code": "cc-peer",
        "opencode": "oc-peer",
        "codex": "codex-peer",
        "agy": "agy-peer",
    }
    for i, (mode, name) in enumerate(modes.items()):
        _write_mode_fleet(
            tmp_path / "profiles" / f"p{i}" / "fleet.yaml",
            name, mode, f"/repos/{mode}",
        )
    results = mod._managed_repos()
    found_modes = {mode for _name, _repo, mode in results}
    assert found_modes == {"claude_code", "opencode", "codex", "agy"}


def test_managed_repos_excludes_unmanaged_and_unknown_mode(monkeypatch, tmp_path):
    """Peers with managed=false or unsupported/missing mode must be excluded."""
    mod = _load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "fleet.yaml").write_text(
        "fleet:\n"
        "  agents:\n"
        "    no-managed:\n"
        "      url: http://x\n"
        "      mode: claude_code\n"
        "      repo_path: /repos/nm\n"
        "    unknown-mode:\n"
        "      url: http://x\n"
        "      managed: true\n"
        "      mode: future_mode\n"
        "      repo_path: /repos/fm\n"
        "    no-mode:\n"
        "      url: http://x\n"
        "      managed: true\n"
        "      repo_path: /repos/nomode\n",
        encoding="utf-8",
    )
    assert mod._managed_repos() == []


def test_transcript_path_per_mode():
    """Each mode resolves to its own transcript filename under .hermes/."""
    mod = _load_module()
    expected = {
        "claude_code": "a2a-transcript.jsonl",
        "opencode": "a2a-oc-transcript.jsonl",
        "codex": "a2a-codex-transcript.jsonl",
        "agy": "a2a-agy-transcript.jsonl",
    }
    for mode, filename in expected.items():
        p = mod._transcript_path("/some/repo", mode)
        assert p == Path("/some/repo/.hermes") / filename, (
            f"mode={mode!r}: expected {filename!r}, got {p.name!r}"
        )


def test_transcript_path_unknown_mode_falls_back_to_claude():
    """An unknown/legacy mode must fall back to the claude_code filename, not crash."""
    mod = _load_module()
    p = mod._transcript_path("/some/repo", "future_unknown_mode")
    assert p.name == "a2a-transcript.jsonl"


@pytest.fixture
def mixed_fleet(monkeypatch, tmp_path):
    """Four repos, one per mode, each with one transcript message."""
    mod = _load_module()
    mode_filename = {
        "claude_code": "a2a-transcript.jsonl",
        "opencode":    "a2a-oc-transcript.jsonl",
        "codex":       "a2a-codex-transcript.jsonl",
        "agy":         "a2a-agy-transcript.jsonl",
    }
    peers = []
    for mode, filename in mode_filename.items():
        repo = tmp_path / mode
        repo.mkdir()
        _write_mode_transcript(repo, filename, [
            {"ts": "2026-06-01 10:00:00", "dir": "hermes->peer",
             "contextId": f"ctx-{mode}", "text": f"hello from {mode}"},
        ])
        peers.append((f"peer-{mode}", str(repo), mode))
    monkeypatch.setattr(mod, "_managed_repos", lambda: peers)
    return mod, peers


def test_mixed_fleet_list_conversations_includes_all_modes(mixed_fleet):
    """list_conversations must surface all 4 modes and include the mode field."""
    mod, peers = mixed_fleet
    res = _run(mod.list_conversations())
    assert res["count"] == 4
    found = {c["mode"] for c in res["conversations"]}
    assert found == {"claude_code", "opencode", "codex", "agy"}
    for conv in res["conversations"]:
        assert "mode" in conv


def test_mixed_fleet_list_peers_includes_all_modes(mixed_fleet):
    """list_peers must surface all 4 modes and include the mode field."""
    mod, peers = mixed_fleet
    res = _run(mod.list_peers())
    assert res["count"] == 4
    found = {p["mode"] for p in res["peers"]}
    assert found == {"claude_code", "opencode", "codex", "agy"}
    for peer in res["peers"]:
        assert "mode" in peer
        assert peer["transcript_exists"] is True
        assert peer["message_count"] == 1


def test_get_conversation_includes_mode(mixed_fleet):
    """get_conversation must include the mode field in the response."""
    mod, _peers = mixed_fleet
    res = _run(mod.get_conversation("ctx-claude_code"))
    assert res["mode"] == "claude_code"


def test_legacy_peer_no_mode_field_does_not_crash(monkeypatch, tmp_path):
    """A peer entry without a mode field must not crash; it is simply excluded."""
    mod = _load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "fleet.yaml").write_text(
        "fleet:\n"
        "  agents:\n"
        "    legacy:\n"
        "      url: http://127.0.0.1:9300\n"
        "      managed: true\n"
        "      repo_path: /repos/legacy\n",
        encoding="utf-8",
    )
    # Must not raise; legacy peer with no mode is excluded (mode=None fails supports_managed_mode).
    result = mod._managed_repos()
    assert result == []
