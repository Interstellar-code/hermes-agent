"""Port-band allocation tests for managed A2A receivers.

Each managed mode owns a contiguous 10-port band so multiple same-mode
receivers (one per repo) can coexist without colliding with another mode's
band. Covered here:

* band/default mapping is the documented layout (cc 9300, oc 9310, codex 9320, agy 9330);
* each deploy module's DEFAULT_BIND_PORT equals its band start (drift guard);
* allocate_band_port skips busy + claimed ports and reports exhaustion;
* resolve_managed_bind_port: explicit honored, existing reused, else auto-pick.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from a2a_fleet import managed_peers


# --- band layout + parity --------------------------------------------------


def test_band_layout_is_documented() -> None:
    assert managed_peers.port_band_for("claude_code") == (9300, 9309)
    assert managed_peers.port_band_for("opencode") == (9310, 9319)
    assert managed_peers.port_band_for("codex") == (9320, 9329)
    assert managed_peers.port_band_for("agy") == (9330, 9339)


def test_default_port_is_band_start() -> None:
    for mode in managed_peers.SUPPORTED_MANAGED_MODES:
        low, _high = managed_peers.port_band_for(mode)
        assert managed_peers.default_port_for(mode) == low


def test_bands_do_not_overlap_and_are_ten_wide() -> None:
    spans = [managed_peers.port_band_for(m) for m in managed_peers.SUPPORTED_MANAGED_MODES]
    for low, high in spans:
        assert high - low + 1 == managed_peers.PORT_BAND_SIZE
    flat = [p for low, high in spans for p in range(low, high + 1)]
    assert len(flat) == len(set(flat)), "port bands overlap"


@pytest.mark.parametrize(
    "mode,module_name",
    [
        ("claude_code", "cc_deploy"),
        ("opencode", "oc_deploy"),
        ("codex", "codex_deploy"),
        ("agy", "agy_deploy"),
    ],
)
def test_deploy_module_default_matches_band_start(mode: str, module_name: str) -> None:
    import importlib

    module = importlib.import_module(f"a2a_fleet.{module_name}")
    assert module.DEFAULT_BIND_PORT == managed_peers.default_port_for(mode), (
        f"{module_name}.DEFAULT_BIND_PORT drifted from the {mode} band start"
    )


def test_port_band_for_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError):
        managed_peers.port_band_for("nonexistent")


# --- allocate_band_port ----------------------------------------------------


def test_allocate_returns_band_start_when_all_free() -> None:
    port = managed_peers.allocate_band_port("codex", probe=lambda _p: True)
    assert port == 9320


def test_allocate_skips_busy_ports() -> None:
    busy = {9320, 9321}
    port = managed_peers.allocate_band_port(
        "codex", probe=lambda p: p not in busy
    )
    assert port == 9322


def test_allocate_skips_claimed_ports() -> None:
    port = managed_peers.allocate_band_port(
        "agy", claimed={9330, 9331}, probe=lambda _p: True
    )
    assert port == 9332


def test_allocate_returns_none_when_band_exhausted() -> None:
    port = managed_peers.allocate_band_port("opencode", probe=lambda _p: False)
    assert port is None


def test_allocate_claimed_and_busy_combine() -> None:
    busy = {9330}
    port = managed_peers.allocate_band_port(
        "agy", claimed={9331, 9332}, probe=lambda p: p not in busy
    )
    assert port == 9333


# --- resolve_managed_bind_port --------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".hermes").mkdir(parents=True)
    (repo / ".git").mkdir()
    return Path(os.path.realpath(str(repo)))


def test_resolve_honors_explicit_port_even_outside_band(tmp_path: Path) -> None:
    from a2a_fleet import cc_deploy

    repo = _make_repo(tmp_path)
    port, err = cc_deploy.resolve_managed_bind_port(repo, "codex", 9999)
    assert err is None
    assert port == 9999


def test_resolve_reuses_existing_configured_port(tmp_path: Path, monkeypatch) -> None:
    from a2a_fleet import cc_deploy, codex_deploy

    repo = _make_repo(tmp_path)
    (repo / ".hermes" / codex_deploy.CONFIG_FILENAME).write_text(
        json.dumps({"bind_port": 9327})
    )
    # No other peers claim ports; a probe that says "all free" must NOT override
    # the reuse path — an existing config wins so re-deploy is idempotent.
    monkeypatch.setattr(managed_peers, "_port_is_free", lambda p, host="127.0.0.1": True)
    port, err = cc_deploy.resolve_managed_bind_port(repo, "codex", None)
    assert err is None
    assert port == 9327


def test_resolve_auto_picks_when_no_config(tmp_path: Path, monkeypatch) -> None:
    from a2a_fleet import cc_deploy

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(cc_deploy, "_ports_claimed_by_other_repos", lambda mode, r: set())
    monkeypatch.setattr(managed_peers, "_port_is_free", lambda p, host="127.0.0.1": True)
    port, err = cc_deploy.resolve_managed_bind_port(repo, "agy", None)
    assert err is None
    assert port == 9330  # agy band start, nothing else claimed


def test_resolve_errors_when_band_exhausted(tmp_path: Path, monkeypatch) -> None:
    from a2a_fleet import cc_deploy

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(cc_deploy, "_ports_claimed_by_other_repos", lambda mode, r: set())
    monkeypatch.setattr(managed_peers, "_port_is_free", lambda p, host="127.0.0.1": False)
    port, err = cc_deploy.resolve_managed_bind_port(repo, "opencode", None)
    assert port is None
    assert err is not None
    assert "9310-9319" in err


def test_claimed_ports_are_cross_mode(monkeypatch) -> None:
    """A managed peer of ANY mode squatting in the target band must be claimed.

    Regression: a claude_code peer historically bound on 9310 (inside the
    opencode band) must block an opencode allocation there — the claim scan is
    cross-mode, not same-mode-only.
    """
    from a2a_fleet import cc_deploy
    import a2a_fleet.fleet_config as fc

    agents = {
        "cc-other": {
            "managed": True, "mode": "claude_code",
            "repo_path": "/other-repo", "url": "http://127.0.0.1:9310",
        },
        "oc-other": {
            "managed": True, "mode": "opencode",
            "repo_path": "/other-repo2", "url": "http://127.0.0.1:9312",
        },
    }
    monkeypatch.setattr(fc, "load_fleet", lambda: {"agents": agents})
    monkeypatch.setattr(cc_deploy, "canonicalize_repo_path", lambda p: (Path(p), None))

    claimed = cc_deploy._ports_claimed_by_other_repos("opencode", Path("/me"))
    assert 9310 in claimed, "a claude_code peer on 9310 must be claimed cross-mode"
    assert 9312 in claimed


def test_claimed_ports_exclude_same_repo_and_mode(monkeypatch) -> None:
    from a2a_fleet import cc_deploy
    import a2a_fleet.fleet_config as fc

    agents = {
        "oc-self": {
            "managed": True, "mode": "opencode",
            "repo_path": "/me", "url": "http://127.0.0.1:9311",
        },
    }
    monkeypatch.setattr(fc, "load_fleet", lambda: {"agents": agents})
    monkeypatch.setattr(cc_deploy, "canonicalize_repo_path", lambda p: (Path(p), None))

    claimed = cc_deploy._ports_claimed_by_other_repos("opencode", Path("/me"))
    assert 9311 not in claimed, "our own (repo, mode) slot must not be self-claimed"


def test_resolve_skips_port_claimed_by_other_repo(tmp_path: Path, monkeypatch) -> None:
    from a2a_fleet import cc_deploy

    repo = _make_repo(tmp_path)
    # Another repo's codex peer already owns the band start; new repo must skip it.
    monkeypatch.setattr(cc_deploy, "_ports_claimed_by_other_repos", lambda mode, r: {9320})
    monkeypatch.setattr(managed_peers, "_port_is_free", lambda p, host="127.0.0.1": True)
    port, err = cc_deploy.resolve_managed_bind_port(repo, "codex", None)
    assert err is None
    assert port == 9321
