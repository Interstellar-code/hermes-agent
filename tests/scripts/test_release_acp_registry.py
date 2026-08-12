"""Tests that scripts/release.py leaves the ACP Registry manifest alone.

The release script used to bump ``acp_registry/agent.json`` in lockstep with
``pyproject.toml``. That was only correct while this repo could publish
``hermes-agent`` to PyPI — it cannot. The PyPI project belongs to upstream
Nous Research, so trusted publishing from this fork fails with
``invalid-publisher``, and the publish workflow has been removed.

Because the manifest's uvx spec is an exact ``==`` pin against PyPI, lockstep
bumping minted pins to versions that never reached the index (0.19.10,
0.19.11), which breaks ``uvx`` for anyone resolving the manifest. These tests
pin the new contract: the release tool must not touch the manifest.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_release_module(monkeypatch, tmp_root: Path):
    """Import scripts/release.py with REPO_ROOT pinned to a temp tree."""
    spec = importlib.util.spec_from_file_location(
        "_release_under_test",
        Path(__file__).resolve().parents[2] / "scripts" / "release.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "REPO_ROOT", tmp_root)
    monkeypatch.setattr(
        module, "ACP_REGISTRY_MANIFEST", tmp_root / "acp_registry" / "agent.json"
    )
    return module


def _write_manifest(root: Path, version: str) -> None:
    manifest_dir = root / "acp_registry"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "agent.json").write_text(
        json.dumps(
            {
                "id": "hermes-agent",
                "name": "Hermes Agent",
                "version": version,
                "description": "test",
                "distribution": {
                    "uvx": {
                        "package": f"hermes-agent[acp]=={version}",
                        "args": ["hermes-acp"],
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_release_module_has_no_acp_manifest_writer(monkeypatch, tmp_path):
    """The lockstep bump helper is gone and must not come back.

    ``_update_acp_registry_versions`` was the only writer of the manifest.
    Reintroducing it would resume minting PyPI pins this fork cannot publish.
    """
    module = _load_release_module(monkeypatch, tmp_path)

    assert not hasattr(module, "_update_acp_registry_versions")


def test_update_version_files_leaves_manifest_untouched(
    monkeypatch, tmp_path
):
    """End-to-end: update_version_files() is what release.py actually calls,
    so it is where the manifest must be left alone."""
    _write_manifest(tmp_path, "0.13.0")
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
    # uv.lock regeneration is covered on its own in test_release_uv_lock.py;
    # stub it out here so this test stays focused on the manifest.
    monkeypatch.setattr(module, "regenerate_uv_lock", lambda semver: None)

    manifest_path = tmp_path / "acp_registry" / "agent.json"
    before = manifest_path.read_text(encoding="utf-8")

    module.update_version_files("0.14.0", "2026-05-21")

    # The rest of the version bump still happens...
    pyproject_text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.14.0"' in pyproject_text

    # ...but the manifest is byte-for-byte unchanged, still pinned to the
    # version that is actually published on PyPI.
    assert manifest_path.read_text(encoding="utf-8") == before
    manifest = json.loads(before)
    assert manifest["version"] == "0.13.0"
    assert manifest["distribution"]["uvx"]["package"] == "hermes-agent[acp]==0.13.0"


def test_update_version_files_bumps_desktop_package_json_inside_repo_root(
    monkeypatch, tmp_path
):
    """The desktop bump must follow a monkeypatched REPO_ROOT, not import time.

    Regression guard: resolving the desktop package.json into a module-level
    constant binds it at import, so a test that redirects REPO_ROOT to a tmp
    tree would silently rewrite the *real* apps/desktop/package.json in the
    working copy instead. That happened once and was only caught because the
    stray version showed up in `git status`.
    """
    real_desktop_pkg = (
        Path(__file__).resolve().parents[2] / "apps" / "desktop" / "package.json"
    )
    real_before = real_desktop_pkg.read_text(encoding="utf-8")

    _write_manifest(tmp_path, "0.13.0")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "hermes-agent"\nversion = "0.13.0"\n', encoding="utf-8"
    )
    version_dir = tmp_path / "hermes_cli"
    version_dir.mkdir()
    (version_dir / "__init__.py").write_text(
        '__version__ = "0.13.0"\n__release_date__ = "2026-05-14"\n',
        encoding="utf-8",
    )
    desktop_dir = tmp_path / "apps" / "desktop"
    desktop_dir.mkdir(parents=True)
    (desktop_dir / "package.json").write_text(
        '{\n  "name": "hermes",\n  "version": "0.13.0"\n}\n', encoding="utf-8"
    )

    module = _load_release_module(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "VERSION_FILE", version_dir / "__init__.py")
    monkeypatch.setattr(module, "PYPROJECT_FILE", tmp_path / "pyproject.toml")
    monkeypatch.setattr(module, "regenerate_uv_lock", lambda semver: None)

    module.update_version_files("0.14.0", "2026-05-21")

    bumped = json.loads((desktop_dir / "package.json").read_text(encoding="utf-8"))
    assert bumped["version"] == "0.14.0"
    # The real checkout must be untouched.
    assert real_desktop_pkg.read_text(encoding="utf-8") == real_before
