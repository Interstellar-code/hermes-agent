"""Runtime version vs installed version on the dashboard status surface.

Regression cover for issue #199: a dashboard started at 0.19.0 was still
reporting that string four days and five version bumps later, because
``web_server`` binds ``__version__`` at import time and the process was never
restarted. On an editable install the code on disk moves underneath a
long-running process, so "what did I import" and "what is on disk" are
genuinely different questions and the response has to answer both.
"""
from __future__ import annotations

import hermes_cli.web_server as web_server


def _reset_cache() -> None:
    web_server._ON_DISK_CACHE["mtime"] = None
    web_server._ON_DISK_CACHE["value"] = None


def _write_init(tmp_path, version: str, release_date: str):
    """Stand in for the installed package's ``__init__.py``."""
    pkg = tmp_path / "hermes_cli"
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text(
        f'__version__ = "{version}"\n__release_date__ = "{release_date}"\n',
        encoding="utf-8",
    )
    return pkg / "web_server.py"


def test_on_disk_version_is_read_from_the_package_file(tmp_path, monkeypatch):
    fake_module = _write_init(tmp_path, "9.9.9", "2099.1.1")
    monkeypatch.setattr(web_server, "__file__", str(fake_module))
    _reset_cache()

    assert web_server._on_disk_version() == {
        "version": "9.9.9",
        "release_date": "2099.1.1",
    }


def test_files_moving_to_B_does_not_claim_B_is_active(tmp_path, monkeypatch):
    """THE regression: process at A, disk at B, response must not claim B.

    ``version``/``runtime_version`` stay at A because that is what is actually
    executing; ``installed_version`` reports B; ``restart_required`` says the
    two disagree. A client can no longer be misled in either direction.
    """
    fake_module = _write_init(tmp_path, "0.19.8", "2026.7.30")
    monkeypatch.setattr(web_server, "__file__", str(fake_module))
    monkeypatch.setattr(web_server, "__version__", "0.19.0")
    monkeypatch.setattr(web_server, "__release_date__", "2026.7.26")
    _reset_cache()

    fields = web_server._version_provenance(web_server._on_disk_version())

    assert fields["runtime_version"] == "0.19.0", "the running code is still A"
    assert fields["installed_version"] == "0.19.8", "disk has moved to B"
    assert fields["restart_required"] is True
    assert fields["version_source"] == "process-import"


def test_matching_versions_do_not_ask_for_a_restart(tmp_path, monkeypatch):
    fake_module = _write_init(tmp_path, "0.19.8", "2026.7.30")
    monkeypatch.setattr(web_server, "__file__", str(fake_module))
    monkeypatch.setattr(web_server, "__version__", "0.19.8")
    _reset_cache()

    fields = web_server._version_provenance(web_server._on_disk_version())
    assert fields["restart_required"] is False


def test_unreadable_package_degrades_to_unknown_not_to_stale(tmp_path, monkeypatch):
    """A parse failure must not nag every client into restarting.

    ``/api/status`` is a public liveness endpoint that uptime probes hit, so an
    unreadable file has to degrade to "unknown" rather than raise or guess.
    """
    monkeypatch.setattr(web_server, "__file__", str(tmp_path / "gone" / "web_server.py"))
    _reset_cache()

    on_disk = web_server._on_disk_version()
    assert on_disk == {"version": None, "release_date": None}

    fields = web_server._version_provenance(on_disk)
    assert fields["installed_version"] is None
    assert fields["restart_required"] is False, "unknown must never mean stale"


def test_cache_refreshes_when_the_file_changes(tmp_path, monkeypatch):
    """An in-place update must be picked up without restarting the dashboard.

    The whole point is to notice a version change in a long-running process, so
    the mtime cache must not pin the first answer forever.
    """
    fake_module = _write_init(tmp_path, "0.19.0", "2026.7.26")
    monkeypatch.setattr(web_server, "__file__", str(fake_module))
    _reset_cache()
    assert web_server._on_disk_version()["version"] == "0.19.0"

    init_py = tmp_path / "hermes_cli" / "__init__.py"
    stale_mtime = init_py.stat().st_mtime
    _write_init(tmp_path, "0.19.8", "2026.7.30")
    import os

    os.utime(init_py, (stale_mtime + 10, stale_mtime + 10))

    assert web_server._on_disk_version()["version"] == "0.19.8"


# ---------------------------------------------------------------------------
# Commit-level staleness: the version alone is not enough
# ---------------------------------------------------------------------------


def test_code_moving_without_a_version_bump_still_requires_restart(
    tmp_path, monkeypatch
):
    """The hole a version-only check leaves, and the reason #200 needs this.

    Strict-mode updates fast-forward by whatever is upstream. Most commits do
    not touch __version__, so a version-only comparison would report "nothing
    to restart" while the process ran code that no longer exists on disk.
    """
    fake_module = _write_init(tmp_path, "0.19.8", "2026.7.30")
    monkeypatch.setattr(web_server, "__file__", str(fake_module))
    monkeypatch.setattr(web_server, "__version__", "0.19.8")  # version did NOT move
    monkeypatch.setattr(web_server, "_RUNTIME_COMMIT", "a" * 40)
    monkeypatch.setattr(web_server, "_head_sha", lambda _root: "b" * 40)
    _reset_cache()

    fields = web_server._version_provenance(web_server._on_disk_version())
    assert fields["restart_required"] is True
    assert fields["restart_reason"] == "commit"
    assert fields["runtime_commit"] == "a" * 40
    assert fields["installed_commit"] == "b" * 40


def test_same_commit_and_version_needs_no_restart(tmp_path, monkeypatch):
    fake_module = _write_init(tmp_path, "0.19.8", "2026.7.30")
    monkeypatch.setattr(web_server, "__file__", str(fake_module))
    monkeypatch.setattr(web_server, "__version__", "0.19.8")
    monkeypatch.setattr(web_server, "_RUNTIME_COMMIT", "a" * 40)
    monkeypatch.setattr(web_server, "_head_sha", lambda _root: "a" * 40)
    _reset_cache()

    fields = web_server._version_provenance(web_server._on_disk_version())
    assert fields["restart_required"] is False
    assert fields["restart_reason"] is None


def test_non_git_install_is_unknown_not_stale(tmp_path, monkeypatch):
    """No SHA on either side must not read as "restart" forever."""
    fake_module = _write_init(tmp_path, "0.19.8", "2026.7.30")
    monkeypatch.setattr(web_server, "__file__", str(fake_module))
    monkeypatch.setattr(web_server, "__version__", "0.19.8")
    monkeypatch.setattr(web_server, "_RUNTIME_COMMIT", None)
    monkeypatch.setattr(web_server, "_head_sha", lambda _root: None)
    _reset_cache()

    fields = web_server._version_provenance(web_server._on_disk_version())
    assert fields["restart_required"] is False
    assert fields["installed_commit"] is None


def test_version_signal_is_reported_even_when_both_moved(tmp_path, monkeypatch):
    """A real release moves both; name the version, it is the useful one."""
    fake_module = _write_init(tmp_path, "0.19.8", "2026.7.30")
    monkeypatch.setattr(web_server, "__file__", str(fake_module))
    monkeypatch.setattr(web_server, "__version__", "0.19.0")
    monkeypatch.setattr(web_server, "_RUNTIME_COMMIT", "a" * 40)
    monkeypatch.setattr(web_server, "_head_sha", lambda _root: "b" * 40)
    _reset_cache()

    fields = web_server._version_provenance(web_server._on_disk_version())
    assert fields["restart_required"] is True
    assert fields["restart_reason"] == "version"


def test_head_sha_reads_a_real_checkout(tmp_path):
    """Read git's files directly — no subprocess at import time."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "--initial-branch=main"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"],
                   check=True, capture_output=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c"], check=True, capture_output=True)

    expected = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert web_server._head_sha(repo) == expected


def test_head_sha_on_a_non_checkout_is_none(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert web_server._head_sha(plain) is None
