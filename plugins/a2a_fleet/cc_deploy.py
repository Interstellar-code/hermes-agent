#!/usr/bin/env python3
"""Deploy + manage a Claude Code A2A executor receiver in a target repo.

This module backs three Hermes tools (registered in ``__init__.py``):

  * ``deploy_cc_receiver(repo_path, bind_port=9300, model=None)`` — copy the
    ``templates/cc_receiver.py`` into ``<repo>/.hermes/``, write the binding
    ``a2a_receiver.json`` (cwd pinned to the canonical repo), inject an
    idempotent ``@import .hermes/A2A.md`` managed block into ``<repo>/CLAUDE.md``,
    stop any old receiver, launch the new one detached, and health-check it.
  * ``cc_receiver_status(repo_path)`` — PID-alive AND ``/health`` (PID alone is
    insufficient: stale pidfiles / PID reuse cause false "running").
  * ``cc_receiver_stop(repo_path)`` — SIGTERM (SIGKILL fallback), remove pidfile.

Design constraints (deliberate):
  * STDLIB ONLY. No import of the receiver template or the a2a_fleet package.
  * Handlers NEVER raise — they return ``{"error": "..."}`` on any failure so the
    Hermes agent can surface deploy/launch problems clearly (Hermes review #5).
  * ``cwd`` for claude is ALWAYS the canonicalized ``repo_path`` written into the
    config — never taken from an inbound message (security; Codex #4).
  * Detached launch uses ``start_new_session=True`` (POSIX setsid equivalent) — we
    do NOT rely on a ``setsid`` binary (macOS lacks it, like ``timeout``).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("a2a_fleet.cc_deploy")

# Managed-block markers injected into <repo>/CLAUDE.md. The block is rewritten
# in place when the markers already exist (idempotent), else appended.
CLAUDE_MD_START = "<!-- a2a-fleet:start -->"
CLAUDE_MD_END = "<!-- a2a-fleet:end -->"
CLAUDE_MD_IMPORT_LINE = "@.hermes/A2A.md"

# Role text written verbatim to <repo>/.hermes/A2A.md and @import-ed by CLAUDE.md.
A2A_ROLE_TEXT = (
    "# A2A Executor Role (managed by Hermes a2a_fleet)\n"
    "\n"
    "You are a Claude Code executor peer in an A2A fleet. Orchestrator: Hermes at "
    "http://127.0.0.1:9219. You receive tasks over A2A and execute them in THIS "
    "repo using your full tools/skills/MCP. Reply concisely with results/status. "
    "The same A2A contextId = the same ongoing session — context accumulates.\n"
)

DEFAULT_BIND_PORT = 9300
DEFAULT_HERMES_URL = "http://127.0.0.1:9219/jsonrpc"
PID_FILENAME = "cc_receiver.pid"
RECEIVER_FILENAME = "cc_receiver.py"
CONFIG_FILENAME = "a2a_receiver.json"
ROLE_FILENAME = "A2A.md"
LOG_FILENAME = "cc_receiver.log"

# Health-check poll budget (~8s) and per-attempt request timeout.
HEALTH_POLL_BUDGET_S = 8.0
HEALTH_POLL_INTERVAL_S = 0.4
HEALTH_REQUEST_TIMEOUT_S = 1.5

# Stop-old grace before SIGKILL.
STOP_TERM_WAIT_S = 3.0
STOP_POLL_INTERVAL_S = 0.1


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def canonicalize_repo_path(repo_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve ``repo_path`` to an absolute canonical dir, rejecting unsafe input.

    Returns ``(canonical_path, None)`` on success, or ``(None, error_message)``.

    Rejects: empty input, non-existent paths, non-directories, and paths whose
    realpath differs from a plain abspath in their existing components (symlink
    escape / non-canonical components). The realpath is the pinned cwd written
    into the receiver config, so it must be the true on-disk location.
    """
    if not repo_path or not str(repo_path).strip():
        return None, "repo_path is empty"
    raw = str(repo_path).strip()
    # Expand ~ but do NOT expand env vars (avoid surprising substitution).
    expanded = os.path.expanduser(raw)
    real = os.path.realpath(expanded)
    if not os.path.exists(real):
        return None, f"repo_path does not exist: {raw}"
    if not os.path.isdir(real):
        return None, f"repo_path is not a directory: {raw}"
    # Symlink-escape / non-canonical guard: the user-supplied path's lexical
    # absolute form must resolve to itself. If realpath rewrote it (a symlink
    # component pointed elsewhere), refuse — we will only write into the TRUE
    # canonical location and the caller must name it directly.
    abspath = os.path.abspath(expanded)
    if os.path.realpath(abspath) != real or abspath != real:
        return None, (
            f"repo_path is not canonical (symlink or non-canonical components): "
            f"{raw} -> {real}"
        )
    return Path(real), None


def _is_git_repo(repo: Path) -> bool:
    """True if ``repo`` looks like a git working tree (``.git`` present)."""
    return (repo / ".git").exists()


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------

def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file in same dir + os.replace)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)  # atomic on POSIX


# ---------------------------------------------------------------------------
# CLAUDE.md managed @import block
# ---------------------------------------------------------------------------

def _managed_block() -> str:
    """The exact managed block (markers + @import line), no surrounding newlines."""
    return f"{CLAUDE_MD_START}\n{CLAUDE_MD_IMPORT_LINE}\n{CLAUDE_MD_END}"


def upsert_claude_md_import(claude_md_path: Path) -> str:
    """Ensure the managed @import block exists in ``CLAUDE.md`` (idempotent).

    * Absent file -> create it containing just the managed block.
    * Markers already present -> replace the block in place (repair stale content
      between markers) without touching anything outside the markers.
    * Markers absent but file has content -> append the block (separated by a
      blank line) preserving all existing content.

    Returns ``"imported"`` (created/appended) or ``"already-imported"`` /
    ``"refreshed"`` for visibility. Atomic write (temp + os.replace).
    """
    block = _managed_block()
    if not claude_md_path.exists():
        _atomic_write_text(claude_md_path, block + "\n")
        return "imported"

    content = claude_md_path.read_text()
    start_idx = content.find(CLAUDE_MD_START)
    end_idx = content.find(CLAUDE_MD_END)

    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        # Replace the existing block in place (markers inclusive). NEVER clobber
        # content before the start marker or after the end marker.
        end_idx_full = end_idx + len(CLAUDE_MD_END)
        before = content[:start_idx]
        after = content[end_idx_full:]
        existing_block = content[start_idx:end_idx_full]
        new_content = before + block + after
        if new_content == content:
            return "already-imported"
        _atomic_write_text(claude_md_path, new_content)
        # If the only change was repairing torn/stale inner content, report it.
        return "already-imported" if existing_block == block else "refreshed"

    # Markers absent: append, preserving existing content. Ensure separation.
    sep = "" if content.endswith("\n\n") else ("\n" if content.endswith("\n") else "\n\n")
    new_content = content + sep + block + "\n"
    _atomic_write_text(claude_md_path, new_content)
    return "imported"


# ---------------------------------------------------------------------------
# Receiver config
# ---------------------------------------------------------------------------

def build_receiver_config(
    repo_path: Path,
    bind_port: int,
    model: Optional[str],
) -> Dict[str, Any]:
    """Build the ``a2a_receiver.json`` payload matching cc_receiver's load_config.

    Keys mirror the template's ``DEFAULTS`` so ``load_config()`` consumes them
    directly. ``role_file`` is a repo-relative path to ``.hermes/A2A.md``; cwd is
    pinned to the canonical ``repo_path``. ``claude_model`` is omitted when no
    model is supplied so the template's own default applies.
    """
    cfg: Dict[str, Any] = {
        "repo_path": str(repo_path),  # canonical, pinned cwd for claude
        "bind_host": "127.0.0.1",
        "bind_port": int(bind_port),
        "hermes_url": DEFAULT_HERMES_URL,
        "role_file": f".hermes/{ROLE_FILENAME}",
        "idle_timeout_s": 1800,
        "max_concurrent_turns": 3,
    }
    if model:
        cfg["claude_model"] = str(model)
    return cfg


# ---------------------------------------------------------------------------
# PID / process control
# ---------------------------------------------------------------------------

def _read_pid(pid_path: Path) -> Optional[int]:
    """Read an int PID from ``pid_path``; None if missing/garbage."""
    try:
        raw = pid_path.read_text().strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` exists (signal 0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def _terminate_pid(pid: int) -> bool:
    """SIGTERM ``pid``, wait briefly, SIGKILL if still alive. Returns True if killed/gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError as exc:
        log.warning("SIGTERM pid=%s failed (%s)", pid, exc)
        return False
    deadline = time.monotonic() + STOP_TERM_WAIT_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(STOP_POLL_INTERVAL_S)
    # Still alive -> SIGKILL.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError as exc:
        log.warning("SIGKILL pid=%s failed (%s)", pid, exc)
        return False
    return not _pid_alive(pid)


def _stop_old_receiver(pid_path: Path) -> Optional[int]:
    """If a live receiver PID is recorded, stop it. Returns the stopped PID or None."""
    pid = _read_pid(pid_path)
    if pid is None or not _pid_alive(pid):
        return None
    log.info("stopping existing receiver pid=%s before redeploy", pid)
    _terminate_pid(pid)
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass
    return pid


# ---------------------------------------------------------------------------
# claude CLI probe
# ---------------------------------------------------------------------------

def _probe_claude_cli() -> bool:
    """Best-effort ``claude --version`` probe. False (warn) if absent/failed."""
    try:
        proc = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.returncode == 0
    except FileNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("claude --version probe failed (%s)", exc)
        return False


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def _health_url(bind_port: int) -> str:
    return f"http://127.0.0.1:{int(bind_port)}/health"


def _check_health_once(bind_port: int) -> bool:
    """Single GET /health probe; True iff HTTP 200 with JSON ``{"ok": true}``."""
    try:
        req = urllib.request.Request(_health_url(bind_port), method="GET")
        with urllib.request.urlopen(req, timeout=HEALTH_REQUEST_TIMEOUT_S) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001 — any failure means not-yet-healthy
        return False
    return isinstance(body, dict) and bool(body.get("ok"))


def _poll_health(bind_port: int, budget_s: float = HEALTH_POLL_BUDGET_S) -> bool:
    """Poll GET /health until healthy or the budget elapses."""
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if _check_health_once(bind_port):
            return True
        time.sleep(HEALTH_POLL_INTERVAL_S)
    return False


# ---------------------------------------------------------------------------
# Detached launch
# ---------------------------------------------------------------------------

def _launch_receiver(repo: Path, receiver_path: Path, log_path: Path) -> int:
    """Launch the receiver detached; return the child PID.

    Uses ``start_new_session=True`` so the receiver outlives the gateway and
    lands in its own session/process group (no reliance on a ``setsid`` binary).
    stdout/stderr are redirected to ``<repo>/.hermes/cc_receiver.log``.
    """
    logf = open(log_path, "ab")  # noqa: SIM115 — fd handed to the child; closed below
    try:
        proc = subprocess.Popen(
            [sys.executable, str(receiver_path)],
            cwd=str(repo),
            stdout=logf,
            stderr=logf,
            start_new_session=True,
        )
    finally:
        logf.close()
    return proc.pid


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def deploy_cc_receiver_handler(
    repo_path: str,
    bind_port: int = DEFAULT_BIND_PORT,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Deploy + launch a Claude Code A2A receiver in ``repo_path``.

    Never raises: returns ``{"error": "..."}`` on any failure, else a result dict
    with ``deployed``/``status``/``pid``/``warnings``.
    """
    warnings: List[str] = []

    # 1. Validate + canonicalize.
    repo, err = canonicalize_repo_path(repo_path)
    if err is not None or repo is None:
        return {"error": err or "invalid repo_path"}

    if not _is_git_repo(repo):
        warnings.append(f"{repo} does not look like a git repo (.git missing)")

    # Locate the bundled template.
    template_path = Path(__file__).parent / "templates" / RECEIVER_FILENAME
    if not template_path.exists():
        return {"error": f"receiver template missing: {template_path}"}

    hermes_dir = repo / ".hermes"
    receiver_dest = hermes_dir / RECEIVER_FILENAME
    role_dest = hermes_dir / ROLE_FILENAME
    config_dest = hermes_dir / CONFIG_FILENAME
    pid_path = hermes_dir / PID_FILENAME
    log_path = hermes_dir / LOG_FILENAME
    claude_md_path = repo / "CLAUDE.md"

    # 2. mkdir -p <repo>/.hermes/
    try:
        hermes_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"error": f"cannot create {hermes_dir} (no write permission?): {exc}"}

    # 3. Copy template -> <repo>/.hermes/cc_receiver.py
    try:
        shutil.copyfile(template_path, receiver_dest)
    except OSError as exc:
        return {"error": f"cannot copy receiver template into {hermes_dir}: {exc}"}

    # 4. Write the executor role text.
    try:
        _atomic_write_text(role_dest, A2A_ROLE_TEXT)
    except OSError as exc:
        return {"error": f"cannot write {role_dest}: {exc}"}

    # 5. Idempotent @import managed block into CLAUDE.md.
    try:
        claude_md_status = upsert_claude_md_import(claude_md_path)
    except OSError as exc:
        return {"error": f"cannot update {claude_md_path}: {exc}"}

    # 6. Write the binding config (cwd pinned to canonical repo).
    try:
        cfg = build_receiver_config(repo, bind_port, model)
        _atomic_write_text(config_dest, json.dumps(cfg, indent=2) + "\n")
    except OSError as exc:
        return {"error": f"cannot write {config_dest}: {exc}"}

    # claude CLI presence (non-fatal; surfaced as a warning per Hermes review #5).
    if not _probe_claude_cli():
        warnings.append("claude CLI not found on PATH (claude --version failed); turns will fail")

    # 7. Stop any old receiver before relaunch (avoid double-bind on the port).
    stopped = _stop_old_receiver(pid_path)
    if stopped is not None:
        warnings.append(f"stopped previous receiver pid={stopped}")

    # 8. Launch detached.
    try:
        pid = _launch_receiver(repo, receiver_dest, log_path)
    except OSError as exc:
        return {"error": f"failed to launch receiver (port {bind_port} in use?): {exc}"}

    # 9. Health-check.
    healthy = _poll_health(int(bind_port))
    if not healthy:
        warnings.append(
            f"health-check failed: GET {_health_url(bind_port)} did not return ok within "
            f"{HEALTH_POLL_BUDGET_S:.0f}s (see {log_path})"
        )

    return {
        "deployed": True,
        "repo_path": str(repo),
        "port": int(bind_port),
        "pid": pid,
        "status": "healthy" if healthy else "unhealthy",
        "claude_md": claude_md_status,
        "warnings": warnings,
    }


async def cc_receiver_status_handler(repo_path: str) -> Dict[str, Any]:
    """Report receiver liveness: PID-alive AND ``/health`` (both required).

    Never raises; returns ``{"error": ...}`` for an invalid repo_path.
    """
    repo, err = canonicalize_repo_path(repo_path)
    if err is not None or repo is None:
        return {"error": err or "invalid repo_path"}

    hermes_dir = repo / ".hermes"
    pid_path = hermes_dir / PID_FILENAME
    config_path = hermes_dir / CONFIG_FILENAME

    pid = _read_pid(pid_path)
    alive = pid is not None and _pid_alive(pid)

    port: Optional[int] = None
    try:
        cfg = json.loads(config_path.read_text())
        if isinstance(cfg, dict) and cfg.get("bind_port") is not None:
            port = int(cfg["bind_port"])
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        port = None

    healthy = bool(port is not None and _check_health_once(port))
    running = bool(alive and healthy)

    return {
        "running": running,
        "pid": pid,
        "port": port,
        "healthy": healthy,
        "repo_path": str(repo),
    }


async def cc_receiver_stop_handler(repo_path: str) -> Dict[str, Any]:
    """Stop the receiver via its PID file (SIGTERM, SIGKILL fallback), remove pidfile.

    Never raises; returns ``{"error": ...}`` for an invalid repo_path.
    """
    repo, err = canonicalize_repo_path(repo_path)
    if err is not None or repo is None:
        return {"error": err or "invalid repo_path"}

    pid_path = repo / ".hermes" / PID_FILENAME
    pid = _read_pid(pid_path)
    if pid is None:
        return {"stopped": False, "pid": None, "detail": "no PID file"}

    if not _pid_alive(pid):
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"stopped": False, "pid": pid, "detail": "process not running"}

    killed = _terminate_pid(pid)
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass
    return {"stopped": bool(killed), "pid": pid}
