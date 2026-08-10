"""Tests for the uv.lock regeneration step in scripts/release.py.

uv.lock embeds hermes-agent's own version as a package entry
(``source = { editable = "." }``). If a release bumps pyproject.toml without
re-running ``uv lock``, the lockfile goes stale and ``uv lock --check``
(invoked by ``uv sync --locked`` in CI) fails at the install step, before a
single test runs -- indistinguishable on the CI dashboard from a real test
failure. Commit 8712d5b7b introduced exactly this drift and the Python test
suite silently did not run for 176 commits. These tests guard the fix:
``update_version_files()`` must always regenerate (and verify) uv.lock, and
must never swallow a failure to do so.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_release_module(monkeypatch, tmp_root: Path):
    """Import scripts/release.py with REPO_ROOT/UV_LOCK_FILE pinned to a temp tree."""
    spec = importlib.util.spec_from_file_location(
        "_release_under_test_uv_lock",
        Path(__file__).resolve().parents[2] / "scripts" / "release.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "REPO_ROOT", tmp_root)
    monkeypatch.setattr(module, "UV_LOCK_FILE", tmp_root / "uv.lock")
    return module


def _write_lock(root: Path, version: str) -> None:
    (root / "uv.lock").write_text(
        f'[[package]]\nname = "hermes-agent"\nversion = "{version}"\n'
        'source = { editable = "." }\n',
        encoding="utf-8",
    )


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeSubprocess:
    """Stand-in for the ``subprocess`` module, scoped to the loaded module.

    ``monkeypatch.setattr(module, "subprocess", fake)`` rebinds the name only
    inside the loaded module's own globals, so this never touches the real
    ``subprocess`` module used elsewhere in the process.
    """

    def __init__(self, result: _FakeCompleted, side_effect=None):
        self.result = result
        self.side_effect = side_effect
        self.calls = []

    def run(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.side_effect is not None:
            return self.side_effect(cmd)
        return self.result


# ── _find_uv_bin ────────────────────────────────────────────────────────


def test_find_uv_bin_prefers_path(monkeypatch, tmp_path):
    module = _load_release_module(monkeypatch, tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/uv")
    assert module._find_uv_bin() == "/usr/bin/uv"


def test_find_uv_bin_falls_back_to_managed_uv(monkeypatch, tmp_path):
    module = _load_release_module(monkeypatch, tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda name: None)

    hermes_home = tmp_path / "hermes_home"
    managed_uv = hermes_home / "bin" / "uv"
    managed_uv.parent.mkdir(parents=True)
    managed_uv.write_text("#!/bin/sh\n")
    managed_uv.chmod(0o755)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert module._find_uv_bin() == str(managed_uv)


def test_find_uv_bin_returns_none_when_nothing_found(monkeypatch, tmp_path):
    module = _load_release_module(monkeypatch, tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nonexistent_home"))
    assert module._find_uv_bin() is None


# ── regenerate_uv_lock ──────────────────────────────────────────────────


def test_regenerate_uv_lock_raises_when_uv_not_found(monkeypatch, tmp_path):
    module = _load_release_module(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "_find_uv_bin", lambda: None)

    with pytest.raises(RuntimeError, match="uv"):
        module.regenerate_uv_lock("1.2.3")


def test_regenerate_uv_lock_raises_when_uv_lock_command_fails(monkeypatch, tmp_path):
    module = _load_release_module(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "_find_uv_bin", lambda: "/fake/uv")
    fake = _FakeSubprocess(_FakeCompleted(returncode=1, stderr="resolution failed"))
    monkeypatch.setattr(module, "subprocess", fake)

    with pytest.raises(RuntimeError, match="resolution failed"):
        module.regenerate_uv_lock("1.2.3")


def test_regenerate_uv_lock_raises_when_lock_still_stale(monkeypatch, tmp_path):
    """`uv lock` exits 0 but the embedded version wasn't actually updated --
    must not be treated as success (this is the exact silent-failure shape
    the original bug had)."""
    module = _load_release_module(monkeypatch, tmp_path)
    _write_lock(tmp_path, "1.0.0")  # stale: still the old version
    monkeypatch.setattr(module, "_find_uv_bin", lambda: "/fake/uv")
    monkeypatch.setattr(module, "subprocess", _FakeSubprocess(_FakeCompleted(returncode=0)))

    with pytest.raises(RuntimeError, match="stale"):
        module.regenerate_uv_lock("1.2.3")


def test_regenerate_uv_lock_raises_when_lock_file_missing_after_success(monkeypatch, tmp_path):
    module = _load_release_module(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "_find_uv_bin", lambda: "/fake/uv")
    monkeypatch.setattr(module, "subprocess", _FakeSubprocess(_FakeCompleted(returncode=0)))
    # No uv.lock written at all.

    with pytest.raises(RuntimeError, match="missing"):
        module.regenerate_uv_lock("1.2.3")


def test_regenerate_uv_lock_succeeds_when_version_matches(monkeypatch, tmp_path):
    module = _load_release_module(monkeypatch, tmp_path)
    fake = _FakeSubprocess(_FakeCompleted(returncode=0))

    def _run_and_update_lock(cmd):
        # Simulate `uv lock` actually rewriting the lockfile in place.
        _write_lock(tmp_path, "1.2.3")
        return _FakeCompleted(returncode=0)

    fake.side_effect = _run_and_update_lock
    monkeypatch.setattr(module, "_find_uv_bin", lambda: "/fake/uv")
    monkeypatch.setattr(module, "subprocess", fake)

    module.regenerate_uv_lock("1.2.3")  # must not raise
    assert fake.calls[0][0] == ["/fake/uv", "lock"]
    assert fake.calls[0][1]["cwd"] == str(tmp_path)


# ── update_version_files wiring ─────────────────────────────────────────


def test_update_version_files_calls_regenerate_uv_lock(monkeypatch, tmp_path):
    """End-to-end regression guard: update_version_files() is the function
    release.py's --publish path actually calls, so it must drive the uv.lock
    regeneration too -- this is the exact omission that caused 176 commits
    of silently-skipped CI (commit 8712d5b7b)."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "hermes-agent"\nversion = "0.13.0"\n', encoding="utf-8"
    )
    version_dir = tmp_path / "hermes_cli"
    version_dir.mkdir()
    (version_dir / "__init__.py").write_text(
        '__version__ = "0.13.0"\n__release_date__ = "2026-05-14"\n',
        encoding="utf-8",
    )

    module = _load_release_module(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "VERSION_FILE", version_dir / "__init__.py")
    monkeypatch.setattr(module, "PYPROJECT_FILE", tmp_path / "pyproject.toml")
    monkeypatch.setattr(
        module, "ACP_REGISTRY_MANIFEST", tmp_path / "acp_registry" / "agent.json"
    )

    calls = []
    monkeypatch.setattr(
        module, "regenerate_uv_lock", lambda semver: calls.append(semver)
    )

    module.update_version_files("0.14.0", "2026-05-21")

    assert calls == ["0.14.0"]


def test_update_version_files_propagates_uv_lock_failure(monkeypatch, tmp_path):
    """A stale/failed uv.lock regeneration must abort update_version_files
    (and therefore the release) rather than being swallowed."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "hermes-agent"\nversion = "0.13.0"\n', encoding="utf-8"
    )
    version_dir = tmp_path / "hermes_cli"
    version_dir.mkdir()
    (version_dir / "__init__.py").write_text(
        '__version__ = "0.13.0"\n__release_date__ = "2026-05-14"\n',
        encoding="utf-8",
    )

    module = _load_release_module(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "VERSION_FILE", version_dir / "__init__.py")
    monkeypatch.setattr(module, "PYPROJECT_FILE", tmp_path / "pyproject.toml")
    monkeypatch.setattr(
        module, "ACP_REGISTRY_MANIFEST", tmp_path / "acp_registry" / "agent.json"
    )

    def _boom(semver):
        raise RuntimeError("uv not found")

    monkeypatch.setattr(module, "regenerate_uv_lock", _boom)

    with pytest.raises(RuntimeError, match="uv not found"):
        module.update_version_files("0.14.0", "2026-05-21")
