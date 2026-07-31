"""Strict, non-destructive updater for API-triggered Hermes updates (#200).

Switch UI can detect updates but cannot safely apply them, because
``POST /api/hermes/update`` runs the full CLI updater, which stashes local
work (``main.py`` stash/apply/drop, default
``update.non_interactive_local_changes: "stash"``). A browser click must never
move somebody's uncommitted work.

WHY THIS IS A SEPARATE MODULE, not a ``--strict`` flag on the existing updater:
the safety property we have to guarantee is a NEGATIVE one — "this path cannot
stash, reset, clean, rebase, checkout, or discard". Threading a flag through
the CLI updater would make that property depend on correct propagation through
thousands of lines and every future edit to them. Here it is a property of one
frozen tuple, ``_ALLOWED_GIT``, checked at the single choke point every git
call goes through. It is provable by reading one screen, and tested directly.

The one mutation this module can perform is ``merge --ff-only``, which by
construction refuses anything that is not a fast-forward and never touches the
working tree when it declines.

Nothing here imports FastAPI: the endpoints are a thin shell over these
functions so the preconditions can be tested against real temporary git
repositories without a web server.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Every git subcommand this module may ever run. The whole safety argument of
# strict mode rests on this tuple: adding a verb here is the only way to make
# strict mode destructive, which makes it a deliberate, reviewable act rather
# than an accident three call-frames deep.
#
# `fetch` is write-only to remote-tracking refs. `merge --ff-only` refuses a
# non-fast-forward instead of resolving it. Everything else is read-only.
_ALLOWED_GIT: Tuple[str, ...] = (
    "rev-parse",
    "status",
    "symbolic-ref",
    "config",
    "remote",
    "merge-base",
    "rev-list",
    "fetch",
    "merge",
    "ls-files",
)

# Verbs that must never appear in _ALLOWED_GIT. Kept as data, next to the
# allowlist, so the guarantee reads as a rule rather than as a comment — and so
# a test can assert the two never intersect.
FORBIDDEN_GIT: Tuple[str, ...] = (
    "stash",
    "reset",
    "clean",
    "rebase",
    "checkout",
    "restore",
    "switch",
    "cherry-pick",
    "revert",
    "am",
    "apply",
    "push",
)

# Bound on `blocking_files`, which is echoed back to a browser. A checkout with
# 40k untracked files must not turn a status response into a payload bomb.
_MAX_BLOCKING_FILES = 20

DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"

# Bumped when the wire contract changes in a way a client must notice. Switch
# UI keys off this rather than off a Hermes version number, so an older agent
# that lacks strict mode simply omits the field.
STRICT_UPDATE_API = 1


class StrictUpdateError(RuntimeError):
    """A strict-mode precondition failed, or git itself did."""

    def __init__(self, state: str, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason


def _git(repo: Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run one allowlisted git subcommand in ``repo``.

    The choke point. ``args[0]`` is checked against ``_ALLOWED_GIT`` before the
    process is spawned, so a destructive verb cannot reach git even if a caller
    asks for one. ``repo`` is server-owned (PROJECT_ROOT) and never client
    input; the SHAs a client sends are compared as strings and never passed
    here as arguments.
    """
    if not args or args[0] not in _ALLOWED_GIT:
        raise StrictUpdateError(
            "internal_error",
            f"strict mode refuses git subcommand {args[0] if args else '<none>'!r}",
        )
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _out(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or "").strip()


def _is_git_checkout(repo: Path) -> bool:
    proc = _git(repo, "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and _out(proc) == "true"


def _current_branch(repo: Path) -> Optional[str]:
    """Branch name, or None when HEAD is detached."""
    proc = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    return _out(proc) if proc.returncode == 0 else None


def _dirty_files(repo: Path) -> List[str]:
    """Paths that make the worktree unclean, INCLUDING untracked ones.

    ``--porcelain`` with the default untracked mode is what makes this stricter
    than a plain "is the index clean" check: an untracked file that the update
    would collide with is exactly the case the CLI updater's stash exists to
    paper over, and strict mode refuses instead.

    Only basenames are returned — the response crosses to a browser and may
    cross hosts, so absolute paths (which leak home directories and deployment
    layout) must not travel.
    """
    proc = _git(repo, "status", "--porcelain", "--untracked-files=all")
    if proc.returncode != 0:
        raise StrictUpdateError("unknown", "git status failed")
    names: List[str] = []
    for line in (proc.stdout or "").splitlines():
        if len(line) > 3:
            names.append(Path(line[3:].strip().strip('"')).name)
    return names


def _remote_url(repo: Path, remote: str) -> Optional[str]:
    proc = _git(repo, "remote", "get-url", remote)
    return _out(proc) if proc.returncode == 0 else None


def _rev(repo: Path, ref: str) -> Optional[str]:
    """Resolve a SERVER-CONSTRUCTED ref. Never call with client input."""
    proc = _git(repo, "rev-parse", "--verify", "--quiet", ref)
    return _out(proc) or None if proc.returncode == 0 else None


def _is_ancestor(repo: Path, maybe_ancestor: str, descendant: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", maybe_ancestor, descendant).returncode == 0


def _count(repo: Path, rev_range: str) -> int:
    proc = _git(repo, "rev-list", "--count", rev_range)
    try:
        return int(_out(proc))
    except ValueError:
        return 0


def preflight(
    repo: Path,
    *,
    remote: str = DEFAULT_REMOTE,
    branch: str = DEFAULT_BRANCH,
    fetch: bool = False,
) -> Dict[str, Any]:
    """Classify the checkout. Read-only except for an optional ``fetch``.

    Returns the capability block the API advertises. ``can_apply_strict`` is
    true for exactly one state — ``clean-behind`` — so a new state added later
    fails closed rather than silently becoming applicable.

    ``fetch`` updates remote-tracking refs only; it never touches HEAD, the
    index, or the working tree.
    """
    result: Dict[str, Any] = {
        "strict_update_api": STRICT_UPDATE_API,
        "can_apply_strict": False,
        "checkout_state": "unknown",
        "current_head": None,
        "target_head": None,
        "behind": None,
        "blocking_files": [],
        "blocking_files_truncated": False,
        "remote": remote,
        "branch": branch,
    }

    try:
        if not _is_git_checkout(repo):
            result["checkout_state"] = "not-a-git-checkout"
            return result

        if _remote_url(repo, remote) is None:
            result["checkout_state"] = "wrong-remote"
            return result

        current_branch = _current_branch(repo)
        if current_branch is None:
            result["checkout_state"] = "detached"
            result["current_head"] = _rev(repo, "HEAD")
            return result
        if current_branch != branch:
            result["checkout_state"] = "unsupported-branch"
            result["current_head"] = _rev(repo, "HEAD")
            return result

        if fetch:
            # Remote-tracking refs only. Deliberately not --prune and not
            # --tags: strict mode's job is to advance one branch, not to
            # reshape the local ref namespace.
            proc = _git(repo, "fetch", remote, branch, timeout=120.0)
            if proc.returncode != 0:
                raise StrictUpdateError("unknown", "git fetch failed")

        head = _rev(repo, "HEAD")
        target = _rev(repo, f"{remote}/{branch}")
        result["current_head"] = head
        result["target_head"] = target
        if head is None or target is None:
            result["checkout_state"] = "unknown"
            return result

        # Dirtiness is reported before the ancestry verdict: a dirty checkout
        # that is also behind is blocked on dirtiness, and that is the more
        # actionable message for whoever has to clear it.
        dirty = _dirty_files(repo)
        if dirty:
            result["checkout_state"] = "dirty"
            result["blocking_files"] = dirty[:_MAX_BLOCKING_FILES]
            result["blocking_files_truncated"] = len(dirty) > _MAX_BLOCKING_FILES
            return result

        if head == target:
            result["checkout_state"] = "clean-uptodate"
            result["behind"] = 0
            return result

        head_is_ancestor = _is_ancestor(repo, head, target)
        target_is_ancestor = _is_ancestor(repo, target, head)
        if head_is_ancestor:
            result["checkout_state"] = "clean-behind"
            result["behind"] = _count(repo, f"HEAD..{remote}/{branch}")
            result["can_apply_strict"] = True
        elif target_is_ancestor:
            result["checkout_state"] = "ahead"
        else:
            result["checkout_state"] = "diverged"
        return result

    except StrictUpdateError as exc:
        result["checkout_state"] = exc.state
        result["reason"] = exc.reason
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        result["checkout_state"] = "unknown"
        result["reason"] = f"git unavailable: {type(exc).__name__}"
        return result


def apply_strict(
    repo: Path,
    *,
    expected_current_head: Optional[str] = None,
    expected_target_head: Optional[str] = None,
    remote: str = DEFAULT_REMOTE,
    branch: str = DEFAULT_BRANCH,
) -> Dict[str, Any]:
    """Fast-forward the checkout, or refuse. Never resolves, never discards.

    The expected-SHA arguments are ASSERTIONS, compared as strings against what
    the server itself resolved. They are never passed to git, so a client
    cannot turn them into a ref, an option, or a command argument.

    Preconditions are evaluated twice on purpose: once before the fetch, and
    again after it. Between a client's check call and its apply call the
    upstream may have moved, and the whole point of the SHA assertions is that
    the client gets what it confirmed or gets an error — never a silent update
    to some newer revision.
    """
    before = preflight(repo, remote=remote, branch=branch, fetch=False)
    if expected_current_head and before.get("current_head") != expected_current_head:
        return {
            "ok": False,
            "phase": "preflight",
            "error": "stale_confirmation",
            "reason": "local HEAD is not what the client confirmed against",
            **before,
        }

    after = preflight(repo, remote=remote, branch=branch, fetch=True)
    if not after.get("can_apply_strict"):
        return {
            "ok": False,
            "phase": "preflight",
            "error": "not_applicable",
            "reason": f"checkout is {after.get('checkout_state')}",
            **after,
        }
    if expected_target_head and after.get("target_head") != expected_target_head:
        # The upstream moved after the client confirmed. Refusing is the
        # feature: applying would ship a revision nobody approved.
        return {
            "ok": False,
            "phase": "preflight",
            "error": "target_moved",
            "reason": "upstream advanced after the client confirmed its target",
            **after,
        }

    target = after["target_head"]
    proc = _git(repo, "merge", "--ff-only", f"{remote}/{branch}", timeout=120.0)
    if proc.returncode != 0:
        # --ff-only declines without touching the working tree, so there is
        # nothing to unwind here. Strict mode has no recovery path by design:
        # recovery is what discards work.
        return {
            "ok": False,
            "phase": "applying",
            "error": "fast_forward_failed",
            "reason": "git refused the fast-forward",
            **preflight(repo, remote=remote, branch=branch, fetch=False),
        }

    final = preflight(repo, remote=remote, branch=branch, fetch=False)
    advanced = final.get("current_head") == target
    return {
        "ok": advanced,
        "phase": "applied" if advanced else "failed",
        "error": None if advanced else "head_did_not_advance",
        "source_advanced": advanced,
        **final,
    }
