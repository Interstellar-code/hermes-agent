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

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil

log = logging.getLogger("a2a_fleet.cc_deploy")

# Managed-block markers injected into <repo>/CLAUDE.md. The block is rewritten
# in place when the markers already exist (idempotent), else appended.
CLAUDE_MD_START = "<!-- a2a-fleet:start -->"
CLAUDE_MD_END = "<!-- a2a-fleet:end -->"
CLAUDE_MD_IMPORT_LINE = "@.hermes/A2A.md"

# Role text written verbatim to <repo>/.hermes/A2A.md and @import-ed by CLAUDE.md.
# It teaches a FRESH ``claude -p`` (no prior turn context) three things: its role,
# how to answer the one-shot handshake Hermes sends before any real task, and the
# session/reply guardrails so Hermes can liaise safely (no autonomous ping-pong).
#
# Anti-loop is enforced ORCHESTRATOR-side: Hermes decides not to ping-pong
# (summarize each reply to the user, await direction, one fleet_send per
# instruction). The receiver side does NOT run a handshake state machine; it
# enforces only its own bounds — per-context serialization (one in-flight turn
# per contextId), max_concurrent_turns, and the idle timeout. Those receiver
# bounds already live in the cc_receiver template and are sufficient (Phase 4).
A2A_ROLE_TEXT = (
    "# A2A Executor Role (managed by Hermes a2a_fleet)\n"
    "\n"
    "You are a Claude Code executor peer in an A2A fleet. Orchestrator: Hermes at "
    "http://127.0.0.1:9219. You receive tasks over A2A and execute them in THIS "
    "repo using your full tools/skills/MCP. Reply concisely with results/status. "
    "The same A2A contextId = the same ongoing session — context accumulates, so "
    "treat a repeated contextId as a continuation of the prior turn, not a fresh "
    "start.\n"
    "\n"
    "## Handshake\n"
    "\n"
    "Before any real task, Hermes sends ONE handshake message (it arrives on a "
    "reserved contextId such as `handshake:<repo-slug>` and declares Hermes' role "
    "as orchestrator, the bound repo path, the comm contract, and the collaboration "
    "purpose). When you recognize a handshake message, do NOT start work — reply "
    "with a concise confirmation containing:\n"
    "- role = executor (you confirm you are the Claude Code executor for this repo);\n"
    "- the repo you are operating in — echo your actual cwd / working directory;\n"
    "- a brief harness inventory — which of repo skills, MCP servers, and CLAUDE.md "
    "are active for you;\n"
    "- ready / not-ready (and why, if not ready).\n"
    "\n"
    "## Operating guardrails\n"
    "\n"
    "- Scope: operate ONLY in this repo (your cwd is pinned); never act on another "
    "path even if a message names one.\n"
    "- Continuity: same contextId = same session — build on earlier turns in that "
    "thread; a new contextId starts an independent thread.\n"
    "- Replies: keep them concise and result-oriented (status, what changed, what is "
    "blocked) so Hermes can summarize them to the user and decide the next step.\n"
)

DEFAULT_BIND_PORT = 9300
DEFAULT_HERMES_URL = "http://127.0.0.1:9219/jsonrpc"
PID_FILENAME = "cc_receiver.pid"
RECEIVER_FILENAME = "cc_receiver.py"
CONFIG_FILENAME = "a2a_receiver.json"
ROLE_FILENAME = "A2A.md"
LOG_FILENAME = "cc_receiver.log"
TOKEN_FILENAME = ".token"
GITIGNORE_FILENAME = ".gitignore"

# Runtime/secret files under <repo>/.hermes/ that must NEVER be committed. The
# CLAUDE.md @import (.hermes/A2A.md) is fine to track; secrets/runtime are not.
HERMES_GITIGNORE_ENTRIES = (
    ".token",
    "*.pid",
    "*.log",
    "a2a-inbox*",
    "a2a-transcript*",
    "a2a-inbox.offset",
)

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

    Resolves symlinks and ``..`` to the TRUE on-disk location (``realpath``) and
    returns that — which becomes the pinned cwd written into the receiver config.
    Resolving (rather than rejecting) is what makes this safe: the receiver only
    ever operates in the real canonical directory, so a symlinked input path
    (common on macOS: ``/tmp`` -> ``/private/tmp``, ``/Volumes`` mounts) is
    accepted and pinned to its real target, not treated as an escape.

    Rejects only: empty input, non-existent paths, and non-directories.
    """
    # Defensive: weaker tool-calling models sometimes NEST the argument as
    # {"repo_path": "..."} (or {"path": "..."}) instead of passing the bare
    # string. Unwrap one level so the tool doesn't dead-end on a model quirk.
    if isinstance(repo_path, dict):
        repo_path = repo_path.get("repo_path") or repo_path.get("path") or ""
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
    # ``real`` is the true canonical location; pin everything to it.
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


def _write_token_file(token_path: Path, token: str) -> None:
    """Persist the receiver token to ``token_path`` with mode 0600 (owner-only).

    Uses ``os.open`` with ``O_CREAT|O_WRONLY|O_TRUNC`` and mode ``0o600`` so the
    secret is never world/group-readable even for an instant; ``os.chmod`` after
    the fact additionally repairs the mode if the file pre-existed with looser
    perms (umask can otherwise relax the create mode).
    """
    fd = os.open(token_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass


def upsert_hermes_gitignore(gitignore_path: Path) -> None:
    """Ensure ``<repo>/.hermes/.gitignore`` ignores runtime/secret files.

    Idempotent: creates the file with all entries when absent, else appends only
    the entries not already present (one per line), preserving any user content.
    The tracked ``A2A.md`` / receiver template are deliberately NOT ignored.
    """
    existing_lines: List[str] = []
    if gitignore_path.exists():
        try:
            existing_lines = gitignore_path.read_text().splitlines()
        except OSError:
            existing_lines = []
    present = {ln.strip() for ln in existing_lines}
    missing = [e for e in HERMES_GITIGNORE_ENTRIES if e not in present]
    if not missing:
        return
    if existing_lines:
        body = "\n".join(existing_lines)
        if not body.endswith("\n"):
            body += "\n"
        body += "\n".join(missing) + "\n"
    else:
        body = "\n".join(HERMES_GITIGNORE_ENTRIES) + "\n"
    _atomic_write_text(gitignore_path, body)


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
    auth_token_env: str = "",
    hermes_auth_token_env: str = "",
) -> Dict[str, Any]:
    """Build the ``a2a_receiver.json`` payload matching cc_receiver's load_config.

    Keys mirror the template's ``DEFAULTS`` so ``load_config()`` consumes them
    directly. ``role_file`` is a repo-relative path to ``.hermes/A2A.md``; cwd is
    pinned to the canonical ``repo_path``. ``claude_model`` is omitted when no
    model is supplied so the template's own default applies.

    ``auth_token_env`` names the env var holding the INBOUND bearer the receiver
    requires on POST /jsonrpc; ``hermes_auth_token_env`` names the env var holding
    the OUTBOUND bearer for replies to an auth-enabled Hermes node. Both are
    written only when non-empty (keys the template's DEFAULTS recognize).
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
    if auth_token_env:
        cfg["auth_token_env"] = str(auth_token_env)
    if hermes_auth_token_env:
        cfg["hermes_auth_token_env"] = str(hermes_auth_token_env)
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
    """True if a process with ``pid`` exists."""
    return bool(psutil.pid_exists(pid))


def _terminate_pid(pid: int) -> bool:
    """SIGTERM ``pid``, wait briefly, SIGKILL if still alive. Returns True if killed/gone."""
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError) as exc:
        log.warning("SIGTERM pid=%s failed (%s)", pid, exc)
        return False
    try:
        proc.terminate()
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError) as exc:
        log.warning("SIGTERM pid=%s failed (%s)", pid, exc)
        return False
    deadline = time.monotonic() + STOP_TERM_WAIT_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(STOP_POLL_INTERVAL_S)
    # Still alive -> SIGKILL. SIGKILL is uncatchable, so a successful delivery
    # guarantees the process dies; it may briefly remain a zombie until reaped,
    # which os.kill(pid, 0) still reports as "alive" — that is NOT "still
    # running", so do not report a false negative. Give it a brief moment to be
    # reaped, then report success.
    try:
        proc.kill()
    except (psutil.NoSuchProcess, ProcessLookupError):
        return True
    except (psutil.Error, OSError) as exc:
        log.warning("SIGKILL pid=%s failed (%s)", pid, exc)
        return False
    reap_deadline = time.monotonic() + 1.0
    while time.monotonic() < reap_deadline:
        if not _pid_alive(pid):
            break
        time.sleep(STOP_POLL_INTERVAL_S)
    return True


def _kill_launched_child(pid: int) -> None:
    """Best-effort teardown of a just-launched receiver child + its process group.

    The child is started with ``start_new_session=True``, so it is the leader of
    its own group; ``killpg`` reaps any orphaned subprocesses too. Falls back to a
    plain SIGTERM/SIGKILL on the pid if the group signal fails.
    """
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    except psutil.Error as exc:
        log.warning("process lookup pid=%s failed (%s); falling back to _terminate_pid", pid, exc)
        _terminate_pid(pid)
        return
    children = proc.children(recursive=True)
    for child in children:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            log.warning("child SIGTERM pid=%s failed (%s)", child.pid, exc)
    try:
        proc.terminate()
    except psutil.NoSuchProcess:
        return
    except psutil.Error as exc:
        log.warning("SIGTERM pid=%s failed (%s); falling back to _terminate_pid", pid, exc)
        _terminate_pid(pid)
        return
    deadline = time.monotonic() + STOP_TERM_WAIT_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(STOP_POLL_INTERVAL_S)
    try:
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                continue
            except psutil.Error as exc:
                log.warning("child SIGKILL pid=%s failed (%s)", child.pid, exc)
        proc.kill()
    except psutil.NoSuchProcess:
        return
    except psutil.Error as exc:
        log.warning("SIGKILL pid=%s failed (%s)", pid, exc)
        _terminate_pid(pid)


def _stop_old_receiver(pid_path: Path) -> Tuple[Optional[int], Optional[str]]:
    """If a live receiver PID is recorded, stop it (fail-closed).

    Returns ``(stopped_pid, None)`` when an old receiver was confirmed dead (or
    there was nothing live to stop -> ``(None, None)``).

    If termination FAILS (process still alive after SIGTERM+SIGKILL+wait), returns
    ``(pid, error_message)`` and DOES NOT remove the pidfile — the caller must
    ABORT the redeploy rather than launch a second receiver that double-binds the
    port / runs two bypassPermissions executors against the same repo.
    """
    pid = _read_pid(pid_path)
    if pid is None or not _pid_alive(pid):
        return None, None
    log.info("stopping existing receiver pid=%s before redeploy", pid)
    if not _terminate_pid(pid):
        return pid, (
            f"could not stop existing receiver (pid {pid}); aborting redeploy"
        )
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass
    return pid, None


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


def _check_health_once(bind_port: int, expected_repo_path: Optional[str] = None) -> bool:
    """Single GET /health probe; True iff HTTP 200 with JSON ``{"ok": true}``.

    When ``expected_repo_path`` is given, the response's ``repo_path`` MUST equal
    it — otherwise an UNRELATED process or a stale receiver bound to the same port
    (e.g. a different repo on :9300) would satisfy the check. Mismatch -> False.
    """
    try:
        req = urllib.request.Request(_health_url(bind_port), method="GET")
        with urllib.request.urlopen(req, timeout=HEALTH_REQUEST_TIMEOUT_S) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001 — any failure means not-yet-healthy
        return False
    if not (isinstance(body, dict) and bool(body.get("ok"))):
        return False
    if expected_repo_path is not None and body.get("repo_path") != expected_repo_path:
        return False
    return True


def _poll_health(
    bind_port: int,
    budget_s: float = HEALTH_POLL_BUDGET_S,
    expected_repo_path: Optional[str] = None,
) -> bool:
    """Poll GET /health until healthy (and identity-matched) or the budget elapses."""
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if _check_health_once(bind_port, expected_repo_path):
            return True
        time.sleep(HEALTH_POLL_INTERVAL_S)
    return False


# ---------------------------------------------------------------------------
# Detached launch
# ---------------------------------------------------------------------------

def _launch_receiver(
    repo: Path,
    receiver_path: Path,
    log_path: Path,
    env: Optional[Dict[str, str]] = None,
) -> int:
    """Launch the receiver detached; return the child PID.

    Uses ``start_new_session=True`` so the receiver outlives the gateway and
    lands in its own session/process group (no reliance on a ``setsid`` binary).
    stdout/stderr are redirected to ``<repo>/.hermes/cc_receiver.log``.

    ``env`` (when supplied) is the FULL environment for the child — used to inject
    the provisioned inbound bearer token (its env var name is recorded in the
    config's ``auth_token_env``) so the receiver starts requiring auth.
    """
    logf = open(log_path, "ab")  # noqa: SIM115 — fd handed to the child; closed below
    try:
        proc = subprocess.Popen(
            [sys.executable, str(receiver_path)],
            cwd=str(repo),
            stdout=logf,
            stderr=logf,
            start_new_session=True,
            env=env,
        )
    finally:
        logf.close()
    return proc.pid


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

RECEIVER_TOKEN_ENV_PREFIX = "A2A_CC_TOKEN_"


def stable_token_env_name(repo: Path) -> str:
    """Deterministic inbound-token env var NAME for a canonical repo path.

    The NAME is stable per repo so it can be referenced persistently from
    fleet.yaml (``token_env: <name>``) — unlike a random-hex name, which changes
    every deploy and can't be wired ahead of time. The token VALUE stays a fresh
    ``secrets.token_urlsafe`` per deploy; only the NAME is stable.

    Scheme: ``A2A_CC_TOKEN_<SLUG>_<HASH12>`` where SLUG is the uppercased final
    path component with non-alphanumerics collapsed to ``_`` (env-var-safe), and
    HASH12 is the first 12 hex chars of the SHA-256 of the canonical path (so two
    repos with the same basename get distinct, stable names with low collision
    probability).
    """
    canonical = str(repo)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", repo.name).strip("_").upper() or "REPO"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12].upper()
    return f"{RECEIVER_TOKEN_ENV_PREFIX}{slug}_{digest}"


async def deploy_cc_receiver_handler(
    repo_path: str,
    bind_port: Optional[int] = None,
    model: Optional[str] = None,
    no_auth: bool = False,
    hermes_auth_token_env: str = "",
    **_injected: Any,  # absorb gateway-injected kwargs (e.g. task_id)
) -> Dict[str, Any]:
    """Deploy + launch a Claude Code A2A receiver in ``repo_path``.

    Never raises: returns ``{"error": "..."}`` on any failure, else a result dict
    with ``deployed``/``status``/``pid``/``warnings``.

    Auth provisioning (security): unless ``no_auth=True`` (loopback dev opt-out),
    a random inbound bearer token is generated and injected into the launched
    child's environment under a unique env var name; the receiver config records
    that var name in ``auth_token_env`` so the receiver REQUIRES the bearer on
    POST /jsonrpc. The token value + env var name are surfaced in the result
    (``receiver_token`` / ``receiver_token_env``) so Hermes can wire fleet_send to
    present it (full fleet.yaml wiring is Phase 3).

    ``hermes_auth_token_env`` (optional) is written into the config so the receiver
    sends ``Authorization: Bearer <token>`` on replies to an auth-enabled Hermes.
    """
    # Dispatch shape: registry.dispatch() calls handler(args, **kwargs) — the
    # WHOLE args dict lands in the first positional (repo_path). Unwrap it so
    # all params are extracted, while still tolerating direct kwarg-style calls.
    if isinstance(repo_path, dict):
        _p = repo_path
        repo_path = _p.get("repo_path") or _p.get("path") or ""
        _bp = _p.get("bind_port")
        if _bp is not None:
            bind_port = int(_bp)
        model = _p.get("model") or model
        no_auth = bool(_p.get("no_auth", no_auth))
        hermes_auth_token_env = _p.get("hermes_auth_token_env") or hermes_auth_token_env
    warnings: List[str] = []

    # 1. Validate + canonicalize.
    repo, err = canonicalize_repo_path(repo_path)
    if err is not None or repo is None:
        return {"error": err or "invalid repo_path"}

    # bind_port=None -> reuse this repo's existing port or auto-pick a free one
    # in the claude_code band (9300-9309); an explicit value is honored verbatim.
    bind_port, port_err = resolve_managed_bind_port(repo, "claude_code", bind_port)
    if port_err is not None:
        return {"error": port_err}

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

    # 6. Provision an inbound bearer token (unless explicitly opted out). The
    # token reaches the child via its env; only the env var NAME goes in the
    # config so the receiver requires the bearer on POST /jsonrpc.
    receiver_token: Optional[str] = None
    receiver_token_env: str = ""
    if not no_auth:
        receiver_token = secrets.token_urlsafe(32)
        # STABLE name (derived from the canonical repo path) so fleet.yaml can
        # reference it persistently; the VALUE is fresh each deploy.
        receiver_token_env = stable_token_env_name(repo)
    else:
        warnings.append(
            "no_auth=True: receiver started WITHOUT an inbound token — POST /jsonrpc "
            "is OPEN (acceptable only on a trusted loopback dev bind)"
        )

    # 7. Write the binding config (cwd pinned to canonical repo).
    try:
        cfg = build_receiver_config(
            repo, bind_port, model,
            auth_token_env=receiver_token_env,
            hermes_auth_token_env=hermes_auth_token_env,
        )
        _atomic_write_text(config_dest, json.dumps(cfg, indent=2) + "\n")
    except OSError as exc:
        return {"error": f"cannot write {config_dest}: {exc}"}

    # claude CLI presence (non-fatal; surfaced as a warning per Hermes review #5).
    if not _probe_claude_cli():
        warnings.append("claude CLI not found on PATH (claude --version failed); turns will fail")

    # 8. Stop any old receiver before relaunch (fail-closed: abort if it survives,
    # else a live old receiver double-binds the port / runs a second executor).
    stopped, stop_err = _stop_old_receiver(pid_path)
    if stop_err is not None:
        return {"error": stop_err}
    if stopped is not None:
        warnings.append(f"stopped previous receiver pid={stopped}")

    # 9. Launch detached, injecting the inbound token into the child's env ONLY.
    # The parent's os.environ and the on-disk .token are NOT mutated yet: if the
    # launch fails or the child never becomes healthy, we must leave no token
    # leak behind (#7). The doomed child still got the token at launch, but it is
    # torn down below, so that copy is harmless.
    # Always pin HERMES_HOME so the detached receiver resolves the SAME profile
    # the deployer did, never the silent ~/.hermes default-profile fallback (#98).
    from hermes_constants import get_hermes_home  # noqa: PLC0415,WPS433

    child_env: Dict[str, str] = dict(os.environ)
    child_env["HERMES_HOME"] = str(get_hermes_home())
    if receiver_token is not None:
        child_env[receiver_token_env] = receiver_token
    try:
        pid = _launch_receiver(repo, receiver_dest, log_path, env=child_env)
    except OSError as exc:
        return {"error": f"failed to launch receiver (port {bind_port} in use?): {exc}"}

    # 10. Health-check WITH identity validation (the receiver's /health echoes its
    # pinned repo_path; an unrelated/stale process on the port must not pass).
    healthy = _poll_health(int(bind_port), expected_repo_path=str(repo))
    if not healthy:
        # Fail-closed: a launched-but-unhealthy child must not be reported as a
        # success. Tear it down (killpg/SIGTERM), drop any pidfile it wrote, error.
        # os.environ / .token were NOT touched on this path -> no token leak (#7).
        _kill_launched_child(pid)
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "error": (
                f"receiver launched but never became healthy on :{int(bind_port)} "
                f"(port in use? startup crash? see {log_path})"
            )
        }

    # 11. Health passed -> commit the token to BOTH the parent environment (so an
    # in-process fleet_send resolves token_env via os.environ this session) and
    # the persisted <repo>/.hermes/.token (chmod 0600) so a gateway RESTART can
    # re-publish the SAME token the surviving receiver was launched with — this is
    # what lets boot-reconcile leave a healthy receiver alone (#1). Also ensure the
    # runtime/secret .gitignore so the token + pid/log never get committed.
    if receiver_token is not None:
        os.environ[receiver_token_env] = receiver_token
        try:
            _write_token_file(hermes_dir / TOKEN_FILENAME, receiver_token)
        except OSError as exc:
            warnings.append(f"could not persist receiver token to .token: {exc}")
    try:
        upsert_hermes_gitignore(hermes_dir / GITIGNORE_FILENAME)
    except OSError as exc:
        warnings.append(f"could not write .hermes/.gitignore: {exc}")

    # 12. Auto-wire the fleet.yaml peer (comment-preserving) so fleet_send can
    # reach this receiver immediately — no hand-editing, no 401. When a token was
    # provisioned the peer is written managed/claude_code/repo_path so a gateway
    # restart re-provisions the SAME token via boot-reconcile. Never fatal: a
    # config-write hiccup is surfaced as a warning, the receiver is already live.
    peer_wiring: Optional[Dict[str, Any]] = None
    try:
        from . import fleet_yaml_io  # noqa: PLC0415,WPS433 — lazy import is the contract.

        peer_url = f"http://127.0.0.1:{int(bind_port)}"
        peer_wiring = fleet_yaml_io.upsert_cc_peer(
            repo_path=str(repo),
            url=peer_url,
            token_env=receiver_token_env,  # "" on no_auth -> plain url peer
        )
        if isinstance(peer_wiring, dict) and peer_wiring.get("error"):
            warnings.append(f"fleet.yaml peer not auto-wired: {peer_wiring['error']}")
    except Exception as exc:  # noqa: BLE001 — never let config-wiring fail the deploy.
        warnings.append(f"fleet.yaml peer not auto-wired: {exc}")

    result: Dict[str, Any] = {
        "deployed": True,
        "repo_path": str(repo),
        "port": int(bind_port),
        "pid": pid,
        "status": "healthy",
        "claude_md": claude_md_status,
        "warnings": warnings,
    }
    if isinstance(peer_wiring, dict) and not peer_wiring.get("error"):
        result["fleet_peer"] = peer_wiring
    # Surface the provisioned inbound token so Hermes can wire fleet_send to send
    # it (Phase 3). Present only when auth was provisioned.
    if receiver_token is not None:
        result["receiver_token"] = receiver_token
        result["receiver_token_env"] = receiver_token_env
    return result


async def cc_receiver_status_handler(repo_path: str, **_injected: Any) -> Dict[str, Any]:
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

    healthy = bool(port is not None and _check_health_once(port, expected_repo_path=str(repo)))
    running = bool(alive and healthy)

    return {
        "running": running,
        "pid": pid,
        "port": port,
        "healthy": healthy,
        "repo_path": str(repo),
    }


async def cc_receiver_stop_handler(repo_path: str, **_injected: Any) -> Dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Boot-reconcile (Phase 3)
# ---------------------------------------------------------------------------



def _managed_receiver_module(mode: str):
    if mode == "claude_code":
        return sys.modules[__name__]
    if mode == "opencode":
        from . import oc_deploy  # noqa: PLC0415,WPS433

        return oc_deploy
    if mode == "codex":
        from . import codex_deploy  # noqa: PLC0415,WPS433

        return codex_deploy
    if mode == "agy":
        from . import agy_deploy  # noqa: PLC0415,WPS433

        return agy_deploy
    raise ValueError(f"unsupported managed receiver mode: {mode!r}")


def _managed_receiver_port(repo: Path, mode: str) -> int:
    module = _managed_receiver_module(mode)
    default = int(getattr(module, "DEFAULT_BIND_PORT"))
    config_filename = str(getattr(module, "CONFIG_FILENAME"))
    try:
        cfg = json.loads((repo / ".hermes" / config_filename).read_text())
        if isinstance(cfg, dict) and cfg.get("bind_port") is not None:
            return int(cfg["bind_port"])
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return default


def _configured_bind_port(repo: Path, mode: str) -> Optional[int]:
    """Bind port recorded in this repo's ``mode`` receiver config, else ``None``.

    Unlike ``_managed_receiver_port`` (which falls back to the mode default),
    this returns ``None`` when no config exists so the caller can tell a
    re-deploy (reuse the stored port) apart from a fresh deploy (allocate one).
    """
    module = _managed_receiver_module(mode)
    config_filename = str(getattr(module, "CONFIG_FILENAME"))
    try:
        cfg = json.loads((repo / ".hermes" / config_filename).read_text())
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
    if isinstance(cfg, dict) and cfg.get("bind_port") is not None:
        try:
            return int(cfg["bind_port"])
        except (ValueError, TypeError):
            return None
    return None


def _ports_claimed_by_other_repos(mode: str, this_repo: Path) -> set:
    """Ports already assigned to OTHER managed peers — ACROSS ALL MODES.

    A TCP port is a port regardless of which mode owns it: a new allocation in
    ``mode``'s band must never land on a port another managed peer holds, even
    a currently-DOWN peer (its socket would test free) or a peer of a DIFFERENT
    mode that sits inside this band (e.g. a legacy/out-of-band entry — a
    claude_code peer historically bound on 9310 would otherwise be handed out
    again to an opencode deploy). We therefore claim every managed peer's port
    and exclude only this exact ``(repo, mode)`` slot (its own reuse is handled
    by ``_configured_bind_port`` upstream).

    Best-effort: a missing/garbled fleet.yaml yields an empty set, and the live
    socket probe in ``allocate_band_port`` remains the backstop.
    """
    claimed: set = set()
    try:
        from . import fleet_config  # noqa: PLC0415,WPS433
        from .managed_peers import iter_supported_managed_peers  # noqa: PLC0415,WPS433

        cfg = fleet_config.load_fleet()
        for _name, entry in iter_supported_managed_peers(cfg.get("agents") or {}):
            peer_mode = str(entry.get("mode") or "")
            peer_repo, err = canonicalize_repo_path(str(entry.get("repo_path")))
            if err is not None or peer_repo is None:
                continue
            if peer_repo == this_repo and peer_mode == mode:
                continue  # our own (repo, mode) slot — reuse handled upstream
            port = _port_from_peer_url(entry.get("url"))
            if port is None:
                port = _configured_bind_port(peer_repo, peer_mode)
            if port is not None:
                claimed.add(int(port))
    except Exception:  # noqa: BLE001 — claim discovery is advisory only.
        pass
    return claimed


def resolve_managed_bind_port(
    repo: Path, mode: str, requested: Optional[int]
) -> Tuple[Optional[int], Optional[str]]:
    """Decide the bind port for a managed receiver deploy.

    * ``requested`` not None -> honored verbatim (explicit manual override; may
      sit outside the band for power users).
    * Re-deploy of a repo that already has a configured port -> that port is
      reused (idempotent: the old receiver on it is torn down + relaunched).
    * Otherwise -> first free port in the mode's band, skipping ports claimed by
      other repos' peers and any port currently bound.

    Returns ``(port, None)`` on success, or ``(None, error)`` when the band is
    exhausted.
    """
    from .managed_peers import allocate_band_port, port_band_for  # noqa: PLC0415

    if requested is not None:
        return int(requested), None

    existing = _configured_bind_port(repo, mode)
    if existing is not None:
        return existing, None

    claimed = _ports_claimed_by_other_repos(mode, repo)
    port = allocate_band_port(mode, claimed=claimed)
    if port is None:
        low, high = port_band_for(mode)
        return None, (
            f"no free port available for mode {mode!r} in band {low}-{high} "
            f"(all {high - low + 1} ports in use); stop an unused {mode} receiver "
            f"or pass an explicit bind_port"
        )
    return port, None


def _read_managed_token_file(repo: Path, mode: str) -> Optional[str]:
    module = _managed_receiver_module(mode)
    token_filename = str(getattr(module, "TOKEN_FILENAME"))
    try:
        raw = (repo / ".hermes" / token_filename).read_text().strip()
    except OSError:
        return None
    return raw or None


def _read_managed_pid(repo: Path, mode: str) -> Optional[int]:
    module = _managed_receiver_module(mode)
    pid_filename = str(getattr(module, "PID_FILENAME"))
    return _read_pid(repo / ".hermes" / pid_filename)


def _deploy_managed_receiver(mode: str, repo: Path, port: int) -> Dict[str, Any]:
    import asyncio

    if mode == "claude_code":
        return asyncio.run(deploy_cc_receiver_handler(str(repo), bind_port=port))
    if mode == "opencode":
        from . import oc_deploy  # noqa: PLC0415,WPS433

        return asyncio.run(oc_deploy.deploy_oc_receiver_handler(str(repo), bind_port=port))
    if mode == "codex":
        from . import codex_deploy  # noqa: PLC0415,WPS433

        return asyncio.run(codex_deploy.deploy_codex_receiver_handler(str(repo), bind_port=port))
    if mode == "agy":
        from . import agy_deploy  # noqa: PLC0415,WPS433

        return asyncio.run(agy_deploy.deploy_agy_receiver_handler(str(repo), bind_port=port))
    raise ValueError(f"unsupported managed receiver mode: {mode!r}")


def _receiver_port(repo: Path, default: int = DEFAULT_BIND_PORT) -> int:
    """Read the receiver's bound port from <repo>/.hermes/a2a_receiver.json."""
    try:
        cfg = json.loads((repo / ".hermes" / CONFIG_FILENAME).read_text())
        if isinstance(cfg, dict) and cfg.get("bind_port") is not None:
            return int(cfg["bind_port"])
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return int(default)


def _port_from_peer_url(url: Optional[str]) -> Optional[int]:
    """Parse the bind port from a peer ``url`` (e.g. http://127.0.0.1:9301 -> 9301).

    Returns ``None`` when the url is absent or carries no explicit port.
    """
    if not url:
        return None
    try:
        from urllib.parse import urlparse  # noqa: PLC0415,WPS433

        port = urlparse(str(url)).port
    except (ValueError, TypeError):
        return None
    return int(port) if port is not None else None


def _read_token_file(repo: Path) -> Optional[str]:
    """Read the persisted receiver token from <repo>/.hermes/.token (None if absent)."""
    try:
        raw = (repo / ".hermes" / TOKEN_FILENAME).read_text().strip()
    except OSError:
        return None
    return raw or None


def reconcile_managed_receivers() -> List[Dict[str, Any]]:
    """On gateway start, leave healthy managed receivers alone; redeploy down ones.

    fleet.yaml is the DESIRED state. For each managed ``claude_code`` or
    ``opencode`` peer with a
    repo_path:
      1. Read the persisted ``<repo>/.hermes/.token`` (if any) and re-publish it
         to ``os.environ[token_env]`` so an in-session ``fleet_send`` presents the
         SAME token the surviving receiver was launched with (H1).
      2. Determine the desired bind port from the peer ``url`` in fleet.yaml; if
         the on-disk receiver config records a different port, prefer
         fleet.yaml and log the drift.
      3. Check health (PID alive + /health + repo_path identity) on that port.
      4. If HEALTHY -> LEAVE IT (action="healthy"). Do NOT redeploy — the running
         executor (possibly mid-task) is preserved and its token is now
         re-published. This fixes restart-kills-in-flight-work (H1).
      5. Only if DOWN/unhealthy -> redeploy (fresh token, rewrites .token,
         relaunches, re-publishes) via the mode-specific deploy handler
         (action="redeployed"). Receiver conversation context survives via the
         mode-specific persistent session state, so a redeploy is safe.

    Never raises: each peer's failure is captured into its summary row. Returns a
    list of ``{agent, repo_path, action, ...}`` rows (one per managed peer).
    """
    from . import fleet_config  # noqa: WPS433 — lazy import is the contract.
    from .managed_peers import iter_supported_managed_peers  # noqa: PLC0415,WPS433

    results: List[Dict[str, Any]] = []
    try:
        cfg = fleet_config.load_fleet()
    except Exception as exc:  # noqa: BLE001 — never raise out of reconcile.
        log.warning("a2a_fleet: boot-reconcile skipped; fleet.yaml not usable (%s)", exc)
        return results

    peers = list(iter_supported_managed_peers(cfg.get("agents") or {}))
    if not peers:
        return results  # common case / fresh installs: nothing to do.

    for name, entry in peers:
        mode = str(entry.get("mode") or "")
        row: Dict[str, Any] = {"agent": name, "repo_path": entry.get("repo_path"), "mode": mode}
        repo, err = canonicalize_repo_path(str(entry.get("repo_path")))
        if err is not None or repo is None:
            row["action"] = "failed"
            row["error"] = err or "invalid repo_path"
            log.warning("a2a_fleet: boot-reconcile %s -> failed (%s)", name, row["error"])
            results.append(row)
            continue

        token_env = entry.get("token_env")
        if not token_env:
            try:
                token_env = _managed_receiver_module(mode).stable_token_env_name(repo)
            except Exception:  # noqa: BLE001
                token_env = stable_token_env_name(repo)

        # (1) Re-publish the persisted token so in-session fleet_send matches the
        # surviving receiver. Only set when present and not already in os.environ
        # with the same value (avoid clobbering a fresher in-process value).
        persisted = _read_managed_token_file(repo, mode)
        if persisted and os.environ.get(token_env) != persisted:
            os.environ[token_env] = persisted

        # (2) Desired port from fleet.yaml url; warn on drift vs on-disk config.
        desired_port = _port_from_peer_url(entry.get("url"))
        on_disk_port = _managed_receiver_port(repo, mode)
        if desired_port is None:
            port = on_disk_port
        else:
            port = desired_port
            if desired_port != on_disk_port:
                log.warning(
                    "a2a_fleet: boot-reconcile %s (%s) port drift — fleet.yaml says :%s but "
                    "on-disk config says :%s; using fleet.yaml",
                    name, mode, desired_port, on_disk_port,
                )

        # (3) Health on the desired port.
        pid = _read_managed_pid(repo, mode)
        alive = pid is not None and _pid_alive(pid)
        healthy = bool(alive and _check_health_once(port, expected_repo_path=str(repo)))

        # (4) Healthy -> leave it (token already re-published above).
        if healthy:
            row["action"] = "healthy"
            log.info("a2a_fleet: boot-reconcile %s -> healthy (pid=%s :%s); left running",
                     name, pid, port)
            results.append(row)
            continue

        # (5) Down/unhealthy -> redeploy on the desired port.
        try:
            res = _deploy_managed_receiver(mode, repo, port)
        except Exception as exc:  # noqa: BLE001
            row["action"] = "failed"
            row["error"] = f"deploy raised: {exc}"
            log.warning("a2a_fleet: boot-reconcile %s -> failed (%s)", name, exc)
            results.append(row)
            continue

        if isinstance(res, dict) and res.get("error"):
            row["action"] = "failed"
            row["error"] = res["error"]
            log.warning("a2a_fleet: boot-reconcile %s -> failed (%s)", name, res["error"])
        else:
            row["action"] = "redeployed"
            row["pid"] = res.get("pid") if isinstance(res, dict) else None
            log.info("a2a_fleet: boot-reconcile %s -> redeployed (pid=%s)", name, row.get("pid"))
        results.append(row)

    return results


# Singleton guard for the boot-reconcile thread. Mirrors __init__'s
# _server_thread / _server_thread_lock pattern so repeated register() calls
# (double plugin load) never spawn racing reconcile threads (#6).
_reconcile_thread: Optional[threading.Thread] = None
_reconcile_lock = threading.Lock()


def reconcile_managed_receivers_in_thread() -> None:
    """Run ``reconcile_managed_receivers`` on a daemon thread (never blocks load).

    Mirrors ``__init__._start_server_in_thread``: the work happens off the plugin
    load path and any failure is swallowed so register() is never disrupted.
    Idempotent — a second call while a reconcile thread is alive is a no-op so
    repeated register() calls don't double-spawn (#6).
    """
    global _reconcile_thread

    def _worker() -> None:
        try:
            reconcile_managed_receivers()
        except Exception:  # noqa: BLE001 — defensive; reconcile already guards.
            log.debug("a2a_fleet: boot-reconcile thread failed", exc_info=True)

    with _reconcile_lock:
        if _reconcile_thread is not None and _reconcile_thread.is_alive():
            log.debug("a2a_fleet: boot-reconcile thread already running, skipping spawn")
            return
        _reconcile_thread = threading.Thread(
            target=_worker,
            name="a2a_fleet.boot_reconcile",
            daemon=True,
        )
        _reconcile_thread.start()
