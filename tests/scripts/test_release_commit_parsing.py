"""The changelog parser must not drop commits.

`get_commits` used to end each record with ``%x00%b%x00`` and split on ``"\0\0"``.
A commit body containing a NUL run merged adjacent records; one ending in a NUL
split a record in half. Either way the changelog silently under-reported -- on
this repo's own history it returned 761 of 2745 commits.

The format is now RS/US delimited. These tests pin both the record count against
git itself and the field integrity, because a format change that is not matched
by a parser change produces exactly one giant record and still "works".
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _release_module():
    spec = importlib.util.spec_from_file_location(
        "release_under_test", REPO_ROOT / "scripts" / "release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _truth(rev: str) -> list[str]:
    out = subprocess.run(
        ["git", "log", f"{rev}..HEAD", "--no-merges", "--format=%H"],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    return out.stdout.split()


@pytest.mark.parametrize("rev", ["HEAD~5", "HEAD~50"])
def test_get_commits_matches_git_exactly(rev):
    """Every commit git reports, in order. Catches the collapse-to-one-record bug."""
    truth = _truth(rev)
    if not truth:
        pytest.skip(f"{rev} unavailable (shallow clone?)")
    got = _release_module().get_commits(rev)
    assert [c["sha"] for c in got] == truth


def test_fields_are_not_polluted_by_separators():
    truth = _truth("HEAD~5")
    if not truth:
        pytest.skip("HEAD~5 unavailable")
    for c in _release_module().get_commits("HEAD~5"):
        assert len(c["sha"]) == 40, f"sha carries stray bytes: {c['sha']!r}"
        for field in ("subject", "author_name", "author_email"):
            assert "\x1e" not in c[field] and "\x1f" not in c[field], \
                f"{field} leaked a separator: {c[field]!r}"


@pytest.mark.parametrize("subject,expected", [
    ("feat(api_server): add thing", "Add thing"),
    ("fix(gateway)!: drop x", "Drop x"),
    ("feat: plain", "Plain"),
    # Unclosed scope: left verbatim beats half-stripping to "Api_server): x".
    ("feat(oops: unclosed", "Feat(oops: unclosed"),
])
def test_clean_subject_strips_scope(subject, expected):
    assert _release_module().clean_subject(subject) == expected


def test_release_targets_the_fork_not_upstream():
    """A hardcoded upstream default minted links to a repo without these commits."""
    mod = _release_module()
    slug = mod.origin_repo_slug()
    assert "/" in slug
    assert f"https://github.com/{slug}" in mod.generate_changelog([], "v1", "1.0")
