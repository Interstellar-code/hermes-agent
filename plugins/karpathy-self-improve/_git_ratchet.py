"""
_git_ratchet.py — Scoped git operations for karpathy-self-improve.

All git commands run with `git -C <profile_root>` so they are strictly
scoped to the given profile directory.  Never raises to the caller —
returns typed result objects instead so the proposer/daemon can handle
errors gracefully.

Result objects are dataclasses with an ``ok: bool`` field and, on
failure, an ``error: str`` field.
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30  # seconds

# ---------------------------------------------------------------------------
# Security helpers — C-1 path containment, C-2 SHA validation
# ---------------------------------------------------------------------------

# All profile repos must live under this root (or a test-patched override).
# Tests may monkeypatch _PROFILES_ROOT to a tmp_path.
_PROFILES_ROOT: Path = Path.home() / ".hermes" / "profiles"

# The default profile root (the hermes home itself, e.g. ~/.hermes).
# Resolved via the canonical source so it respects HERMES_HOME.
# Tests may monkeypatch _DEFAULT_HOME to a tmp_path.
try:
    from hermes_constants import get_default_hermes_root as _get_default_hermes_root
    _DEFAULT_HOME: Path = _get_default_hermes_root()
except Exception:  # pragma: no cover — bare-test / import-unavailable contexts
    _DEFAULT_HOME = Path.home() / ".hermes"

# Exactly 40 lowercase hex chars — git SHA-1.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _assert_contained(root: Path, relpath: str) -> None:
    """Raise ValueError if *relpath* escapes *root* (path traversal guard)."""
    resolved = (root / relpath).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        raise ValueError(
            f"Path traversal detected: {relpath!r} escapes {root}"
        )


def _assert_profile_root(root: Path) -> None:
    """Raise ValueError if *root* is not under _PROFILES_ROOT or equal to _DEFAULT_HOME.

    Named profiles live under _PROFILES_ROOT.
    The "default" profile IS _DEFAULT_HOME (e.g. ~/.hermes itself).
    Both are accepted; all other paths are rejected.
    """
    resolved = root.resolve()
    # Accept: default home (equality check — the root IS the hermes home)
    if resolved == _DEFAULT_HOME.resolve():
        return
    # Accept: named profile under profiles root
    try:
        resolved.relative_to(_PROFILES_ROOT.resolve())
        return
    except ValueError:
        pass
    raise ValueError(
        f"profile_root {root!r} is not inside {_PROFILES_ROOT} "
        f"and is not the default home {_DEFAULT_HOME}"
    )


def _validate_sha(sha: str, label: str = "commit_sha") -> None:
    """Raise ValueError if *sha* is not a 40-hex-char git SHA-1."""
    if not _SHA_RE.fullmatch(sha):
        raise ValueError(
            f"Invalid {label}: {sha!r}. Must be a 40-hex-char SHA-1."
        )


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class GitResult:
    ok: bool
    error: str = ""


@dataclass
class ShaResult(GitResult):
    sha: str = ""


@dataclass
class ApplyResult(GitResult):
    commit_sha: str = ""
    base_sha: str = ""


@dataclass
class RevertResult(GitResult):
    revert_sha: str = ""


@dataclass
class ConflictResult(GitResult):
    conflict: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run(args: List[str], root: Path, input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(root),
        capture_output=True,
        timeout=_GIT_TIMEOUT,
        input=input_bytes,
    )


def _is_git_repo(root: Path) -> bool:
    try:
        r = _run(["git", "-C", str(root), "rev-parse", "--git-dir"], root)
        return r.returncode == 0
    except Exception:
        return False


def _guard(root: Path) -> Optional[str]:
    """Return an error string if *root* is not a git repo, else None."""
    if not root.is_dir():
        return f"profile_root does not exist: {root}"
    if not _is_git_repo(root):
        return f"not a git repo: {root}"
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def current_commit_sha(root: Path) -> ShaResult:
    """Return the HEAD commit SHA for the repo at *root*."""
    err = _guard(root)
    if err:
        return ShaResult(ok=False, error=err)
    try:
        r = _run(["git", "-C", str(root), "rev-parse", "HEAD"], root)
        if r.returncode != 0:
            return ShaResult(ok=False, error=r.stderr.decode(errors="replace").strip())
        return ShaResult(ok=True, sha=r.stdout.decode().strip())
    except Exception as exc:
        return ShaResult(ok=False, error=str(exc))


def blob_sha(root: Path, relpath: str) -> ShaResult:
    """Return the git blob SHA for *relpath* at HEAD."""
    err = _guard(root)
    if err:
        return ShaResult(ok=False, error=err)
    try:
        r = _run(
            ["git", "-C", str(root), "rev-parse", f"HEAD:{relpath}"],
            root,
        )
        if r.returncode != 0:
            return ShaResult(ok=False, error=r.stderr.decode(errors="replace").strip())
        return ShaResult(ok=True, sha=r.stdout.decode().strip())
    except Exception as exc:
        return ShaResult(ok=False, error=str(exc))


def file_hash(root: Path, relpath: str) -> ShaResult:
    """Return a SHA-256 hash of the on-disk content of *relpath* (not git blob SHA).

    Used for manual-conflict detection — compare before-apply vs current.
    """
    err = _guard(root)
    if err:
        return ShaResult(ok=False, error=err)
    try:
        full = root / relpath
        if not full.is_file():
            return ShaResult(ok=False, error=f"file not found: {full}")
        digest = hashlib.sha256(full.read_bytes()).hexdigest()
        return ShaResult(ok=True, sha=digest)
    except Exception as exc:
        return ShaResult(ok=False, error=str(exc))


def detect_manual_conflict(root: Path, relpath: str, expected_blob_sha: str) -> ConflictResult:
    """Return True if the on-disk file no longer matches *expected_blob_sha* (git blob).

    This detects out-of-band manual edits between apply_and_commit and verify.
    """
    err = _guard(root)
    if err:
        return ConflictResult(ok=False, error=err)
    try:
        current = blob_sha(root, relpath)
        if not current.ok:
            # File may have been deleted; treat as conflict.
            return ConflictResult(ok=True, conflict=True)
        conflict = current.sha != expected_blob_sha
        return ConflictResult(ok=True, conflict=conflict)
    except Exception as exc:
        return ConflictResult(ok=False, error=str(exc))


def apply_and_commit(
    root: Path,
    relpath: str,
    new_content: bytes,
    message: str,
) -> ApplyResult:
    """Write *new_content* to *relpath*, stage ONLY that file, and commit.

    Guards:
    - Refuses if other staged changes exist (would pollute the commit).
    - Captures base commit SHA before writing.
    - Returns the new commit SHA and the base SHA.
    """
    # C-1: validate profile root and relpath containment before any write.
    try:
        _assert_profile_root(root)
        _assert_contained(root, relpath)
    except ValueError as exc:
        return ApplyResult(ok=False, error=str(exc))

    err = _guard(root)
    if err:
        return ApplyResult(ok=False, error=err)
    try:
        # Capture base SHA before any changes.
        base = current_commit_sha(root)
        if not base.ok:
            return ApplyResult(ok=False, error=f"cannot get HEAD sha: {base.error}")

        # Refuse if there are already staged changes (someone else staged files).
        r = _run(["git", "-C", str(root), "diff", "--cached", "--name-only"], root)
        if r.returncode != 0:
            return ApplyResult(ok=False, error=r.stderr.decode(errors="replace").strip())
        staged = r.stdout.decode().strip()
        if staged:
            return ApplyResult(
                ok=False,
                error=f"Refusing to commit: other staged changes present: {staged}",
            )

        # Write the file.
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(new_content)

        # Stage exactly this one file.
        r = _run(["git", "-C", str(root), "add", "--", relpath], root)
        if r.returncode != 0:
            return ApplyResult(ok=False, error=r.stderr.decode(errors="replace").strip())

        # Commit.
        r = _run(
            ["git", "-C", str(root), "commit", "-m", message],
            root,
        )
        if r.returncode != 0:
            # Unstage on failure.
            _run(["git", "-C", str(root), "reset", "HEAD", "--", relpath], root)
            return ApplyResult(ok=False, error=r.stderr.decode(errors="replace").strip())

        new_sha = current_commit_sha(root)
        if not new_sha.ok:
            return ApplyResult(ok=False, error=f"committed but cannot get new sha: {new_sha.error}")

        return ApplyResult(ok=True, commit_sha=new_sha.sha, base_sha=base.sha)
    except Exception as exc:
        return ApplyResult(ok=False, error=str(exc))


def revert_commit(root: Path, commit_sha: str, message: str) -> RevertResult:
    """Revert *commit_sha* via `git revert --no-edit` and return the revert commit SHA.

    Falls back to a plain `git restore` + commit if revert produces a conflict.
    """
    # C-1: validate profile root containment before any git operation.
    try:
        _assert_profile_root(root)
    except ValueError as exc:
        return RevertResult(ok=False, error=str(exc))

    # C-2: validate SHA format before passing it to git.
    try:
        _validate_sha(commit_sha)
    except ValueError as exc:
        return RevertResult(ok=False, error=str(exc))

    err = _guard(root)
    if err:
        return RevertResult(ok=False, error=err)
    try:
        r = _run(
            ["git", "-C", str(root), "revert", "--no-edit", "--", commit_sha],
            root,
        )
        if r.returncode == 0:
            sha = current_commit_sha(root)
            if not sha.ok:
                return RevertResult(ok=False, error=f"revert ok but cannot get sha: {sha.error}")
            return RevertResult(ok=True, revert_sha=sha.sha)

        # Revert failed (merge conflict etc.) — abort and signal error.
        _run(["git", "-C", str(root), "revert", "--abort"], root)
        return RevertResult(
            ok=False,
            error=r.stderr.decode(errors="replace").strip() or "git revert failed",
        )
    except Exception as exc:
        return RevertResult(ok=False, error=str(exc))
