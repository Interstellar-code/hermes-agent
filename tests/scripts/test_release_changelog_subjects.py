"""Tests for conventional-commit prefix stripping in scripts/release.py.

``clean_subject()`` turns a raw commit subject into the bullet text that ships
in the public GitHub release notes. It used to strip the prefix with
``^(feat|fix|...)[\\s:(!]+``, which matched the type plus a *single* character
from the separator class. For a scoped commit that character was the opening
paren, so ``feat(api_server): emit approvals`` was stripped to
``api_server): emit approvals`` and then title-cased into
``Api_server): emit approvals``.

Every scoped commit shipped that way — the v0.19.9 and v0.19.10 release notes
are both visibly affected. These tests pin the corrected behaviour so the
mangling cannot silently return.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_release_module():
    """Import scripts/release.py without executing main()."""
    spec = importlib.util.spec_from_file_location(
        "_release_under_test",
        Path(__file__).resolve().parents[2] / "scripts" / "release.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def release():
    return _load_release_module()


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        # The exact regression: scoped commits kept the dangling `):`.
        (
            "feat(api_server): emit approvals on the sessions chat stream",
            "Emit approvals on the sessions chat stream",
        ),
        (
            "fix(release): regenerate uv.lock and stage every file the bump touches",
            "Regenerate uv.lock and stage every file the bump touches",
        ),
        (
            "docs(agents): record the uv.lock drift and gh GraphQL pitfalls",
            "Record the uv.lock drift and gh GraphQL pitfalls",
        ),
        # Scopes containing punctuation must not truncate the subject early.
        (
            "test(gateway/api): cover the retry path",
            "Cover the retry path",
        ),
        # Breaking-change marker, with and without a scope.
        ("feat(api)!: drop the v1 routes", "Drop the v1 routes"),
        ("feat!: drop the v1 routes", "Drop the v1 routes"),
        # Unscoped prefixes keep working exactly as before.
        ("fix: resolve the deadlock", "Resolve the deadlock"),
        ("chore: bump version to 0.19.10", "Bump version to 0.19.10"),
    ],
)
def test_clean_subject_strips_full_conventional_prefix(release, subject, expected):
    assert release.clean_subject(subject) == expected


def test_clean_subject_never_leaves_a_dangling_scope_paren(release):
    """The signature of the old bug: a stray `)` surviving into the notes."""
    for subject in (
        "feat(api_server): emit approvals",
        "fix(workflow-engine): clear the blocking check",
        "test(hermes_cli): make catalog tests hermetic",
    ):
        assert ")" not in release.clean_subject(subject)


def test_clean_subject_leaves_malformed_prefix_verbatim(release):
    """An unclosed scope is left alone rather than half-stripped.

    Half-stripping is what produced the mangled notes; emitting the subject
    intact is the safer failure mode. The unconditional capitalization at the
    end of clean_subject() still applies, so only the prefix is preserved.
    """
    assert release.clean_subject("feat(oops: no closing paren") == (
        "Feat(oops: no closing paren"
    )


def test_clean_subject_does_not_touch_plain_subjects(release):
    """Subjects with no conventional prefix pass through (just capitalized)."""
    assert release.clean_subject("emit approvals on the stream") == (
        "Emit approvals on the stream"
    )
