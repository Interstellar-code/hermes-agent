"""scripts/check_fork_drops.py catches fork features silently dropped by an
adopt+replay upgrade: a def added by a fork commit that no longer exists
anywhere post-adopt (pass 1), and a distinctive log string dropped from
inside a function that still exists (pass 2)."""
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_fork_drops.py"

ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
}


def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], check=True,
                        capture_output=True, text=True, env=ENV)
    return r.stdout.strip()


def _run(repo, before, after, fork_base, allowlist):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--before", before,
         "--after", after, "--fork-base", fork_base, "--allowlist", str(allowlist)],
        capture_output=True, text=True,
    )


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    mod = repo / "mod.py"

    mod.write_text("def existing():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "upstream base")
    fork_base = _git(repo, "rev-parse", "HEAD")

    mod.write_text(
        "def existing():\n    pass\n\n\n"
        "def lost_feature():\n"
        '    logger.warning("distinctive lost feature message across the upgrade")\n'
        "    return True\n"
    )
    _git(repo, "commit", "-aqm", "fork: add lost_feature")
    before = _git(repo, "rev-parse", "HEAD")

    mod.write_text("def existing():\n    pass\n")
    _git(repo, "commit", "-aqm", "adopt: replace tree wholesale")
    after = _git(repo, "rev-parse", "HEAD")

    return repo, fork_base, before, after


def test_drop_detected_and_allowlist_suppresses_it(tmp_path):
    repo, fork_base, before, after = _make_repo(tmp_path)
    allowlist = tmp_path / "allowlist.txt"

    result = _run(repo, before, after, fork_base, allowlist)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "lost_feature" in result.stdout
    assert "distinctive lost feature message across the upgrade" in result.stdout

    allowlist.write_text(
        "lost_feature  # test: intentionally dropped\n"
        "distinctive lost feature message across the upgrade  # test: intentionally dropped\n"
    )
    result = _run(repo, before, after, fork_base, allowlist)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No unexplained fork drops found." in result.stdout
