"""Shared plumbing for the extracted dashboard routers — thin wrappers over the
late-binding seam in :mod:`hermes_cli.web_deps` (web_server owns helpers/state;
every access resolves at call time so ``monkeypatch.setattr(<owning module>, ...)`` wins)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException

from hermes_cli.web_deps import LateState, late
from hermes_cli.web_server_profiles import _profile_cli_args

# Same logger the handlers used before extraction (identical logger object).
log = logging.getLogger("hermes_cli.web_server")

_profile_scope = late("_profile_scope", "hermes_cli.web_server_profiles")
_spawn_hermes_action = late("_spawn_hermes_action", "hermes_cli.web_server_gateway")
# Config read-modify-write serialization for off-loop handlers (live lock —
# LateState supports ``with``-blocks).
_CONFIG_MUTATION_LOCK = LateState("_CONFIG_MUTATION_LOCK")


@contextlib.contextmanager
def config_write_scope(profile: Optional[str]):
    """Profile scope, then the config mutation lock — the write-path nesting
    every config-mutating handler uses."""
    with _profile_scope(profile):
        with _CONFIG_MUTATION_LOCK:
            yield


async def scoped_to_thread(profile: Optional[str], fn: Callable[[], Any]) -> Any:
    """Run ``fn()`` inside ``_profile_scope(profile)`` on a worker thread."""

    def _run():
        with _profile_scope(profile):
            return fn()

    return await asyncio.to_thread(_run)


@contextlib.contextmanager
def http_failure(log_msg: str, status: int, prefix: Optional[str] = None, *, detail: Optional[str] = None):
    """Map unexpected exceptions to an ``HTTPException``.

    ``HTTPException`` passes through; anything else is logged with ``log_msg`` (traceback),
    then re-raised as ``HTTPException(status, f"{prefix}: {exc}")`` — or ``detail`` when given
    (fixed message, exception text only in the log).
    """
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        log.exception(log_msg)
        raise HTTPException(status_code=status, detail=detail if detail is not None else f"{prefix}: {exc}")


def spawn_profile_action(
    profile: Optional[str], argv: list, name: str, *, log_msg: str, prefix: str
) -> dict:
    """Spawn a background ``hermes -p <profile> <argv>`` action; a spawn
    failure is logged and becomes ``500 "<prefix>: <exc>"``."""
    with http_failure(log_msg, 500, prefix):
        proc = _spawn_hermes_action(_profile_cli_args(profile) + argv, name)
    return {"ok": True, "pid": proc.pid, "name": name}


def require(value: Optional[str], detail: str) -> str:
    """Strip ``value``; 400 with ``detail`` when empty."""
    stripped = (value or "").strip()
    if not stripped:
        raise HTTPException(status_code=400, detail=detail)
    return stripped


# Corrupt-store reporting for polled read endpoints. The dashboard polls analytics every few
# seconds; a persistently malformed state.db once produced ~520K identical tracebacks in 24 h
# (#96591). One WARNING per store per interval, then debug; the caller gets an explicit status
# instead of a 500. The file is never quarantined or renamed from here — that is `hermes doctor`'s job.
_CORRUPT_STORE_WARN_INTERVAL_S = 300.0
_corrupt_store_warned_at: Dict[str, float] = {}  # {db path: monotonic}

CORRUPT_STORE_DETAIL = {
    "error": "state_db_corrupt",
    "message": "state.db corrupt — run `hermes doctor` (then `hermes doctor --fix` or `hermes sessions repair`).",
}


@contextlib.contextmanager
def corrupt_store_as_status(db_path):
    """Map a corrupt-image ``sqlite3.DatabaseError`` from a state.db read to a 503 status
    payload, warning once per store per :data:`_CORRUPT_STORE_WARN_INTERVAL_S`.
    Busy/locked and every other error propagate unchanged."""
    from hermes_state_errors import is_malformed_db_error

    try:
        yield
    except sqlite3.DatabaseError as exc:
        if not is_malformed_db_error(exc):
            raise
        key, now = str(db_path), time.monotonic()
        last = _corrupt_store_warned_at.get(key)
        if last is None or now - last >= _CORRUPT_STORE_WARN_INTERVAL_S:
            _corrupt_store_warned_at[key] = now
            log.warning("state.db at %s is corrupt (%s); dashboard reads return a status payload until it is "
                        "repaired — run `hermes doctor`", db_path, exc)
        else:
            log.debug("state.db at %s still corrupt: %s", db_path, exc)
        raise HTTPException(status_code=503, detail={**CORRUPT_STORE_DETAIL, "path": key}) from exc


# --- Version provenance (#199) ---------------------------------------------
#
# An editable install the code on disk can move underneath a long-running process
# for days: the reporting installation had a dashboard started at 0.19.0 still
# serving that string after the checkout had reached 0.19.8, five bumps later.
#
# The fix is not to re-read ``version`` — a client asking a remote dashboard
# what it is running wants the RUNTIME answer, and the process really is running
# the old code. The fix is to make the answer self-describing, so no client has
# to guess which of the two it received. Shared here (rather than on ``status``
# or ``actions``) because both routers need it (``/api/status`` and the update
# endpoints).
_VERSION_RE = re.compile(
    r"^__(version|release_date)__\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE
)
_ON_DISK_CACHE: Dict[str, Any] = {"mtime": None, "value": None}
_RUNTIME_COMMIT_CACHE: Dict[str, Optional[str]] = {}


def _project_root() -> Path:
    """``PROJECT_ROOT`` lives on ``web_server`` (app-assembly module). Imported
    lazily (call time, not module load time): ``web_server`` imports this
    router package while it executes, so a module-level import here would
    race that and risk a partial-module ImportError depending on which side
    imports first."""
    import hermes_cli.web_server as ws

    return ws.PROJECT_ROOT


def _head_sha(repo_root: Path) -> Optional[str]:
    """Checkout HEAD, by reading git's files directly. None when not a checkout.

    Deliberately no subprocess: this may run at request time on a hot polled
    endpoint, and shelling out (slow FS, missing git, a hung filesystem) for a
    field that is only informational is a needless hazard.
    """
    try:
        head = (repo_root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head or None  # detached HEAD holds the SHA directly
    ref = head[4:].strip()
    try:
        return (repo_root / ".git" / ref).read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    try:  # packed refs — a freshly cloned checkout has no loose ref file
        for line in (repo_root / ".git" / "packed-refs").read_text(
            encoding="utf-8"
        ).splitlines():
            if line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    except OSError:
        pass
    return None


def _runtime_commit() -> Optional[str]:
    """Commit this process actually loaded, cached on first access.

    Cached rather than captured at import time: routes are not served until
    the app (and thus ``PROJECT_ROOT``) is fully constructed, so the first
    call already happens no earlier than "process actually running" would.
    """
    if "value" not in _RUNTIME_COMMIT_CACHE:
        _RUNTIME_COMMIT_CACHE["value"] = _head_sha(_project_root())
    return _RUNTIME_COMMIT_CACHE["value"]


def on_disk_version() -> Dict[str, Optional[str]]:
    """Read ``__version__``/``__release_date__`` from the package on disk.

    Parsed textually rather than imported or reloaded: re-importing
    ``hermes_cli`` into a live process would rebind a module other code already
    holds references to, which is a far worse problem than a stale string.

    Cached on the file's mtime because ``/api/status`` is polled. Any failure
    yields ``None`` values — an unreadable file must degrade this to "unknown",
    never break the liveness endpoint that uptime probes depend on.
    """
    init_py = Path(__file__).resolve().parent.parent / "__init__.py"
    try:
        mtime = init_py.stat().st_mtime
    except OSError:
        return {"version": None, "release_date": None}

    if _ON_DISK_CACHE["mtime"] == mtime and _ON_DISK_CACHE["value"] is not None:
        return _ON_DISK_CACHE["value"]

    found: Dict[str, Optional[str]] = {"version": None, "release_date": None}
    try:
        for key, value in _VERSION_RE.findall(init_py.read_text(encoding="utf-8")):
            found[key] = value
    except OSError:
        return {"version": None, "release_date": None}

    _ON_DISK_CACHE["mtime"] = mtime
    _ON_DISK_CACHE["value"] = found
    return found


def version_provenance(on_disk: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """Fields that let a caller tell a stale process from a current one.

    Staleness is decided on TWO signals, because the version alone is not
    enough. Most commits do not bump ``__version__``, so a checkout that has
    moved by several commits can leave the version string identical while the
    process runs code that no longer exists on disk. Either signal disagreeing
    means stale. Neither being readable means unknown, and unknown never means
    stale: a parse failure or a non-git install must not nag every client into
    restarting.
    """
    from hermes_cli import __release_date__, __version__

    installed = on_disk.get("version")
    installed_commit = _head_sha(_project_root())
    runtime_commit = _runtime_commit()

    version_moved = bool(installed) and installed != __version__
    commit_moved = bool(runtime_commit and installed_commit) and (
        installed_commit != runtime_commit
    )

    return {
        "runtime_version": __version__,
        "runtime_release_date": __release_date__,
        "runtime_commit": runtime_commit,
        "installed_version": installed,
        "installed_release_date": on_disk.get("release_date"),
        "installed_commit": installed_commit,
        "version_source": "process-import",
        "restart_required": version_moved or commit_moved,
        # Which signal fired, so a client can explain *why* to a human rather
        # than just showing a restart nag.
        "restart_reason": (
            "version" if version_moved else "commit" if commit_moved else None
        ),
    }
