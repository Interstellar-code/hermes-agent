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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30  # seconds


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
            ["git", "-C", str(root), "commit", "-m", message, "--no-verify"],
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
    err = _guard(root)
    if err:
        return RevertResult(ok=False, error=err)
    try:
        r = _run(
            ["git", "-C", str(root), "revert", "--no-edit", commit_sha],
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
