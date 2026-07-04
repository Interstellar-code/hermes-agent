#!/usr/bin/env python3
"""Standalone A2A receiver — Claude Code as a repo-scoped executor peer.

This file is a TEMPLATE. Hermes' ``deploy_cc_receiver`` tool copies it verbatim
into a target repo's ``<repo>/.hermes/cc_receiver.py`` and writes a sibling
``a2a_receiver.json`` config. The receiver then runs as a detached daemon that:

  1. Serves an A2A surface on ``bind_host:bind_port``
     (GET /health, GET /.well-known/agent-card.json, POST /jsonrpc).
  2. Queues inbound messages to an inbox JSONL and ACKs immediately.
  3. A background poll loop drains the inbox, spawning ``claude -p`` with the
     FULL repo harness (CLAUDE.md, .mcp.json, .claude settings, skills, plugins)
     inherited via ``cwd=repo_path`` + ``--setting-sources user,project,local``.
  4. Maintains a persistent claude session per A2A ``contextId`` via
     ``--session-id`` (first turn) / ``--resume`` (subsequent turns).
  5. POSTs the result back to ``hermes_url`` as a JSON-RPC SendMessage.

Design constraints (deliberate):
  * STDLIB ONLY (+ the ``claude`` CLI). No import of the a2a_fleet package, no
    Hermes gateway dependency — it must run on its own inside any repo.
  * ``cwd`` for claude is ALWAYS ``repo_path`` from config, NEVER taken from an
    inbound message (a remote peer must not be able to redirect execution).
  * Per-contextId serialization: two concurrent turns on the same contextId must
    NOT both spawn ``claude -p --resume <same>`` (that races/corrupts the
    session). A per-contextId lock serializes same-context turns;
    different contextIds run concurrently.

Reply contract (receiver -> Hermes), POSTed to ``hermes_url``::

    {"jsonrpc": "2.0", "id": "cc-<ts>", "method": "SendMessage",
     "params": {"message": {"role": "agent",
                            "parts": [{"text": "<result>"}],
                            "contextId": "<same contextId>"}}}
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "a2a_receiver.json"
INBOX_PATH = SCRIPT_DIR / "a2a-inbox.jsonl"
INBOX_OFFSET_PATH = SCRIPT_DIR / "a2a-inbox.offset"
TRANSCRIPT_PATH = SCRIPT_DIR / "a2a-transcript.jsonl"
PID_PATH = SCRIPT_DIR / "cc_receiver.pid"

# Cap on a single inbound JSON-RPC body (DoS guard) and the prompt we hand to
# claude. 1 MiB body is generous for text tasks; oversized bodies are rejected
# with HTTP 413 before allocation.
MAX_BODY_BYTES = 1 * 1024 * 1024
MAX_PROMPT_CHARS = 256 * 1024
# Cap claude stdout we buffer in memory (defensive — runaway tool output).
MAX_STDOUT_BYTES = 8 * 1024 * 1024

# A signal in a result frame / stderr that a session genuinely does not exist
# (the ONLY condition under which we retry the other session mode — see
# ``run_claude_turn``). Kept narrow on purpose: this receiver runs autonomously
# with ``--permission-mode bypassPermissions``, so a spurious retry can
# double-execute a side-effecting turn.
SESSION_NOT_FOUND_SIGNALS = (
    "no conversation found",
    "session not found",
    "no session found",
    "could not find session",
    "no such session",
)

# uuid5 namespace for deterministic session ids derived from contextId.
SESSION_NAMESPACE = uuid.NAMESPACE_URL

DEFAULTS: Dict[str, Any] = {
    "repo_path": str(SCRIPT_DIR.parent),  # .hermes/ is inside the repo
    "bind_host": "127.0.0.1",
    "bind_port": 9300,
    "hermes_url": "http://127.0.0.1:9219/jsonrpc",
    "role_prompt": (
        "You are a Claude Code executor peer in an A2A fleet. The orchestrator "
        "is Hermes. You receive tasks over A2A and execute them in THIS repo "
        "using your full tools/skills/MCP. Reply concisely with results/status. "
        "Same contextId = same ongoing session/thread."
    ),
    "role_file": None,            # if set, read role prompt from this path (overrides role_prompt)
    "claude_model": "sonnet",
    "claude_extra_flags": [],     # list[str] appended verbatim to the command
    "auth_token_env": None,       # env var name holding the INBOUND bearer token (POST /jsonrpc)
    "hermes_auth_token_env": None,  # env var name holding the bearer token for OUTBOUND replies to Hermes
    "poll_interval_s": 2.0,
    "claude_timeout_s": 300,
    "context_lock_wait_s": 600.0,  # how long a queued same-context turn waits for the lock
    "max_concurrent_turns": 3,     # global cap on simultaneous claude subprocesses
    "max_tracked_contexts": 1024,  # bound on the per-context lock + seen registries
    "idle_timeout_s": 1800,        # self-teardown after this many idle seconds (0 = disabled)
}

log = logging.getLogger("cc_receiver")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(config_path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """Read ``a2a_receiver.json`` (sibling of this script), merge over DEFAULTS.

    Missing / malformed config is non-fatal: defaults are used and a warning is
    logged. ``role_file`` (if set + readable) supplies the role prompt.
    """
    cfg = dict(DEFAULTS)
    cfg["claude_extra_flags"] = list(DEFAULTS["claude_extra_flags"])
    try:
        raw = json.loads(config_path.read_text())
        if isinstance(raw, dict):
            for key, val in raw.items():
                if val is not None:
                    cfg[key] = val
        else:
            log.warning("config %s is not a JSON object; using defaults", config_path)
    except FileNotFoundError:
        log.warning("config %s not found; using defaults", config_path)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("config %s unreadable (%s); using defaults", config_path, exc)

    # role_file overrides role_prompt when present + readable.
    role_file = cfg.get("role_file")
    if role_file:
        try:
            cfg["role_prompt"] = Path(role_file).read_text().strip()
        except OSError as exc:
            log.warning("role_file %s unreadable (%s); using role_prompt", role_file, exc)

    if not isinstance(cfg.get("claude_extra_flags"), list):
        log.warning("claude_extra_flags is not a list; ignoring")
        cfg["claude_extra_flags"] = []

    return cfg


def resolve_auth_token(cfg: Dict[str, Any]) -> Optional[str]:
    """Return the INBOUND bearer token from ``auth_token_env`` env var, or None."""
    env_name = cfg.get("auth_token_env")
    if not env_name:
        return None
    token = os.environ.get(env_name)
    return token or None


def resolve_hermes_auth_token(cfg: Dict[str, Any]) -> Optional[str]:
    """Return the OUTBOUND bearer token (for replies to Hermes) or None.

    Read from the env var named in ``hermes_auth_token_env``. Unset name or
    unset/empty value -> None (no Authorization header sent; current behavior).
    """
    env_name = cfg.get("hermes_auth_token_env")
    if not env_name:
        return None
    token = os.environ.get(env_name)
    return token or None


# ---------------------------------------------------------------------------
# Session id (deterministic per contextId)
# ---------------------------------------------------------------------------

def session_id_for_context(context_id: str) -> str:
    """Deterministic uuid5 session id for a given A2A contextId.

    Same contextId -> same uuid (stable across turns / restarts).
    Distinct contexts -> distinct uuids.
    """
    return str(uuid.uuid5(SESSION_NAMESPACE, "a2a:" + context_id))


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------

# Flags that cc_receiver manages itself — must never be injected via claude_extra_flags.
_FORBIDDEN_CC: frozenset[str] = frozenset({
    "--session-id",
    "--resume",
    "-p", "--print",
    "--output-format",
    "--permission-mode",
    "--model",
    "--setting-sources",
})


def _sanitize_extra_flags(extra: List[str]) -> List[str]:
    """Return a copy of ``extra`` with flags managed by cc_receiver removed.

    Handles both ``--flag value`` (two tokens) and ``--flag=value`` (one token).
    Logs a warning per dropped token so operators notice stale configs.
    """
    result: List[str] = []
    tokens = [str(x) for x in extra]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        base = tok.split("=", 1)[0] if "=" in tok else tok
        if base in _FORBIDDEN_CC:
            log.warning("dropping forbidden claude_extra_flags token %r", tok)
            i += 1
            # --flag value form: also consume the following value token if it
            # does not look like a flag itself.
            if "=" not in tok and i < len(tokens) and not tokens[i].startswith("-"):
                log.warning("dropping forbidden claude_extra_flags value token %r", tokens[i])
                i += 1
            continue
        result.append(tok)
        i += 1
    return result


def build_claude_command(
    prompt: str,
    session_uuid: str,
    cfg: Dict[str, Any],
    *,
    resume: bool,
    mcp_config_path: Optional[Path] = None,
) -> List[str]:
    """Build the ``claude -p`` argv for one turn.

    * ``resume=False`` -> ``--session-id <uuid>`` (first turn for this context).
    * ``resume=True``  -> ``--resume <uuid>`` (subsequent turns).
    * ``--mcp-config`` is appended ONLY when ``mcp_config_path`` is provided
      (the caller decides presence by probing ``<repo>/.mcp.json``).
    * NO ``--bare`` (would disable CLAUDE.md + hooks — the opposite of the goal).
    """
    cmd: List[str] = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--setting-sources",
        "user,project,local",
        "--model",
        str(cfg.get("claude_model") or DEFAULTS["claude_model"]),
        "--append-system-prompt",
        str(cfg.get("role_prompt") or DEFAULTS["role_prompt"]),
    ]
    if mcp_config_path is not None:
        cmd += ["--mcp-config", str(mcp_config_path)]
    if resume:
        cmd += ["--resume", session_uuid]
    else:
        cmd += ["--session-id", session_uuid]

    extra = cfg.get("claude_extra_flags") or []
    if isinstance(extra, list):
        cmd += _sanitize_extra_flags(extra)
    return cmd


def resolve_mcp_config(repo_path: Path) -> Optional[Path]:
    """Return ``<repo>/.mcp.json`` if it exists and is readable JSON, else None.

    Degradation (Codex hardening): absent -> skip flag (log debug). Present but
    malformed/unreadable -> log warning + skip the flag (continue without it).
    Never crash the turn over MCP config.
    """
    mcp_path = repo_path / ".mcp.json"
    if not mcp_path.exists():
        log.debug("no .mcp.json in %s; running without --mcp-config", repo_path)
        return None
    try:
        json.loads(mcp_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(".mcp.json present but unusable (%s); skipping --mcp-config", exc)
        return None
    return mcp_path


# ---------------------------------------------------------------------------
# Deterministic result parsing
# ---------------------------------------------------------------------------

def parse_claude_output(stdout: str) -> Optional[str]:
    """Deterministically extract the reply text from ``stream-json --verbose``.

    Selection order:
      1. The FINAL ``{"type":"result"}`` frame. If that frame signals an error
         (``is_error`` truthy, or ``subtype`` other than ``"success"``), return
         an ``"[error] ..."`` string rather than its (possibly empty) result.
      2. Fallback: the last ``assistant`` message's concatenated text blocks.
      3. None if nothing usable was found.

    Never grabs the last raw line.
    """
    final_result: Optional[Dict[str, Any]] = None
    last_assistant_text: Optional[str] = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        frame_type = obj.get("type")
        if frame_type == "result":
            final_result = obj  # keep overwriting -> ends on the last result frame
        elif frame_type == "assistant":
            text = _assistant_text(obj)
            if text:
                last_assistant_text = text

    if final_result is not None:
        is_error = bool(final_result.get("is_error"))
        subtype = final_result.get("subtype")
        if is_error or (subtype is not None and subtype != "success"):
            detail = final_result.get("result") or subtype or "unknown error"
            return f"[error] claude turn failed: {detail}"
        result_text = final_result.get("result")
        if isinstance(result_text, str) and result_text.strip():
            return result_text.strip()
        # result frame had no usable text — fall through to assistant fallback.

    if last_assistant_text:
        return last_assistant_text.strip()
    return None


def _assistant_text(frame: Dict[str, Any]) -> str:
    """Concatenate text blocks from an ``assistant`` stream-json frame."""
    message = frame.get("message")
    if not isinstance(message, dict):
        return ""
    parts: List[str] = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            txt = block.get("text")
            if isinstance(txt, str):
                parts.append(txt)
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# Per-contextId serialization
# ---------------------------------------------------------------------------

class ContextLocks:
    """Hand out one ``threading.Lock`` per contextId, with bounded eviction.

    Same contextId -> same lock (so its turns serialize). Different contextIds
    -> different locks (so they run concurrently). A registry mutex guards lock
    creation / eviction only; it is NEVER held while a turn runs.

    The registry is bounded to ``max_entries`` via LRU eviction. A lock is only
    evicted if it is NOT currently held (``lock.locked()`` is False) — evicting a
    held lock would let a concurrent same-context turn create a fresh lock and
    race ``--resume`` on the same session. If every candidate is held we exceed
    the bound temporarily rather than corrupt serialization.
    """

    def __init__(self, max_entries: int = 1024) -> None:
        self._registry: "OrderedDict[str, threading.Lock]" = OrderedDict()
        self._mutex = threading.Lock()
        self._max_entries = max(1, int(max_entries))

    def get(self, context_id: str) -> threading.Lock:
        with self._mutex:
            lock = self._registry.get(context_id)
            if lock is None:
                lock = threading.Lock()
                self._registry[context_id] = lock
            self._registry.move_to_end(context_id)  # mark most-recently-used
            self._evict_locked()
            return lock

    def _evict_locked(self) -> None:
        """Evict least-recently-used UNHELD locks until within the bound.

        Caller must hold ``self._mutex``. Held locks are skipped (never evicted),
        so the registry may briefly exceed ``max_entries`` if all overflow
        candidates are actively running.
        """
        if len(self._registry) <= self._max_entries:
            return
        overflow = len(self._registry) - self._max_entries
        evicted = 0
        # Iterate oldest-first; skip held locks.
        for cid in list(self._registry.keys()):
            if evicted >= overflow:
                break
            lk = self._registry[cid]
            if lk.locked():
                continue
            del self._registry[cid]
            evicted += 1

    def size(self) -> int:
        with self._mutex:
            return len(self._registry)


# ---------------------------------------------------------------------------
# Turn execution
# ---------------------------------------------------------------------------

class SeenContexts:
    """Bounded set of contextIds that already started a claude session.

    Used to pick ``--resume`` vs ``--session-id`` (first turn). Bounded via LRU
    so it cannot grow without limit on a long-lived receiver. Eviction here is
    safe regardless of held state: a wrongly-evicted context simply gets treated
    as a first turn, which the narrowed session-retry (see ``run_claude_turn``)
    corrects on a genuine not-found signal.
    """

    def __init__(self, max_entries: int = 1024) -> None:
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._mutex = threading.Lock()
        self._max_entries = max(1, int(max_entries))

    def has(self, context_id: str) -> bool:
        with self._mutex:
            if context_id in self._seen:
                self._seen.move_to_end(context_id)
                return True
            return False

    def mark(self, context_id: str) -> None:
        with self._mutex:
            self._seen[context_id] = None
            self._seen.move_to_end(context_id)
            while len(self._seen) > self._max_entries:
                self._seen.popitem(last=False)

    def size(self) -> int:
        with self._mutex:
            return len(self._seen)


# The runner contract is ``(cmd, cwd, timeout) -> (stdout, rc, stderr)``.
# (stderr is appended in v0.3 so the poll loop can surface a snippet on failure;
#  legacy 2-tuple runners are still accepted for backward compatibility.)


class ClaudeCLINotFound(Exception):
    """Raised by the runner when the ``claude`` binary is not on PATH."""


def run_claude_turn(
    prompt: str,
    context_id: str,
    cfg: Dict[str, Any],
    *,
    runner: Any = None,
    seen: Optional["SeenContexts"] = None,
) -> Optional[str]:
    """Run one claude turn for ``context_id`` with narrowed session retry.

    ``runner`` is an injectable callable ``(cmd, cwd, timeout) -> (stdout, rc)``
    or ``(stdout, rc, stderr)`` used by tests to stub the subprocess. Defaults to
    the real subprocess call.

    Session strategy: pick resume-vs-first from ``seen``. We retry the OTHER mode
    once ONLY on a genuine session-not-found signal (result frame / stderr match
    against ``SESSION_NOT_FOUND_SIGNALS``). A bare ``rc != 0`` does NOT trigger a
    retry: this receiver runs ``--permission-mode bypassPermissions`` and a
    spurious second turn could double-execute a side-effecting action.
    """
    if runner is None:
        runner = _subprocess_runner
    if seen is None:
        seen = SeenContexts()

    repo_path = Path(cfg["repo_path"])
    session_uuid = session_id_for_context(context_id)
    mcp_config = resolve_mcp_config(repo_path)
    timeout = float(cfg.get("claude_timeout_s") or DEFAULTS["claude_timeout_s"])

    first_resume = seen.has(context_id)
    attempts = [first_resume, not first_resume]
    last_reply: Optional[str] = None

    for resume in attempts:
        cmd = build_claude_command(
            prompt, session_uuid, cfg, resume=resume, mcp_config_path=mcp_config
        )
        try:
            stdout, rc, stderr = _call_runner(runner, cmd, str(repo_path), timeout)
        except subprocess.TimeoutExpired:
            return f"[error] claude turn timed out after {timeout}s"
        except (FileNotFoundError, ClaudeCLINotFound):
            # Fatal + distinct: do NOT make this look transient/retryable.
            return "[error] claude CLI not found on PATH"
        except Exception as exc:  # noqa: BLE001 - never crash the poll loop
            log.warning("claude invocation failed (%s)", exc)
            return f"[error] claude invocation failed: {exc}"

        reply = parse_claude_output(stdout)
        # Only retry on a TRUE session-not-found signal, and only once.
        if (
            resume != attempts[-1]
            and _is_session_not_found(reply, stderr)
        ):
            log.info("session mode %s reported not-found for ctx=%s; retrying other mode",
                     "resume" if resume else "first", context_id)
            continue

        if reply is None and rc != 0:
            # No parseable frames + non-zero rc: surface a stderr snippet so the
            # failure is diagnosable rather than a generic "[no reply produced]".
            snippet = (stderr or "").strip().replace("\n", " ")[:300]
            reply = (
                f"[error] claude exited rc={rc} with no parseable output"
                + (f": {snippet}" if snippet else "")
            )
        last_reply = reply
        seen.mark(context_id)
        return reply

    seen.mark(context_id)
    return last_reply


def _call_runner(runner: Any, cmd: List[str], cwd: str, timeout: float) -> Tuple[str, int, str]:
    """Invoke a runner that may return a 2-tuple (legacy) or 3-tuple (with stderr)."""
    result = runner(cmd, cwd, timeout)
    if isinstance(result, tuple) and len(result) == 3:
        stdout, rc, stderr = result
        return stdout, rc, (stderr or "")
    stdout, rc = result  # type: ignore[misc]
    return stdout, rc, ""


def _is_session_not_found(reply: Optional[str], stderr: str) -> bool:
    """True only on a genuine session-not-found signal (reply text or stderr).

    Deliberately narrow: we do NOT treat a bare non-zero rc or a None reply as a
    session error, because retrying the other mode would re-run the turn.
    """
    haystacks: List[str] = []
    if reply:
        haystacks.append(reply.lower())
    if stderr:
        haystacks.append(stderr.lower())
    for hay in haystacks:
        for sig in SESSION_NOT_FOUND_SIGNALS:
            if sig in hay:
                return True
    return False


def _subprocess_runner(cmd: List[str], cwd: str, timeout: float) -> Tuple[str, int, str]:
    """Real subprocess invocation of ``claude -p``.

    Uses ``Popen`` + ``start_new_session=True`` so the whole process tree (claude
    plus any MCP servers / tool subprocesses) lands in its own process group; on
    timeout we ``killpg`` the group to reap orphans. Returns (stdout, rc, stderr).
    Buffers are capped (``MAX_STDOUT_BYTES``) defensively.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise ClaudeCLINotFound("claude") from None
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _killpg(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        raise
    rc = proc.returncode
    if stdout and len(stdout) > MAX_STDOUT_BYTES:
        stdout = stdout[:MAX_STDOUT_BYTES]
    if rc != 0 and stderr:
        log.warning("claude stderr: %s", stderr.strip()[:500])
    return stdout or "", rc, stderr or ""


def _killpg(proc: "subprocess.Popen[Any]") -> None:
    """SIGKILL the whole process group of ``proc`` (best-effort)."""
    killpg = getattr(os, "killpg", None)
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    if killpg is None:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        killpg(os.getpgid(proc.pid), sigkill)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        log.warning("killpg failed for pid=%s (%s); falling back to kill", proc.pid, exc)
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Transcript + reply
# ---------------------------------------------------------------------------

# One lock per append-only file. ThreadingHTTPServer + many turn threads append
# concurrently; without serialization interleaved writes tear JSONL lines and a
# [queued]-ACKed message can be silently lost. These guard the WHOLE append.
_TRANSCRIPT_LOCK = threading.Lock()
_INBOX_LOCK = threading.Lock()


def _append_jsonl(path: Path, rec: Dict[str, Any], lock: threading.Lock) -> None:
    """Append one JSON record + newline atomically wrt other threads on ``lock``."""
    line = json.dumps(rec) + "\n"
    with lock:
        with path.open("a") as f:
            f.write(line)
            f.flush()


def _transcript(direction: str, frm: str, to: str, context_id: str, text: str) -> None:
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dir": direction,
        "from": frm,
        "to": to,
        "contextId": context_id,
        "text": text,
    }
    try:
        _append_jsonl(TRANSCRIPT_PATH, rec, _TRANSCRIPT_LOCK)
    except OSError as exc:
        log.warning("transcript write failed (%s)", exc)


POST_REPLY_MAX_ATTEMPTS = 3
POST_REPLY_BACKOFF_S = 0.5


def post_reply(
    hermes_url: str,
    context_id: str,
    text: str,
    auth_token: Optional[str] = None,
) -> bool:
    """POST the reply back to Hermes as JSON-RPC SendMessage. Returns success.

    When ``auth_token`` is supplied, an ``Authorization: Bearer <token>`` header
    is sent (an auth-enabled Hermes node would 401 otherwise). When None, no
    Authorization header is sent (open loopback dev node — current behavior).

    A transient Hermes outage would drop a completed result, so we retry up to
    ``POST_REPLY_MAX_ATTEMPTS`` with short backoff before giving up (in-process
    only — NOT a durable queue). Final failure is logged loudly and swallowed
    (never crashes the poll loop).
    """
    payload = {
        "jsonrpc": "2.0",
        "id": f"cc-{int(time.time() * 1000)}",
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "agent",
                "parts": [{"text": text}],
                "contextId": context_id,
            }
        },
    }
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    data = json.dumps(payload).encode()

    last_exc: Optional[Exception] = None
    for attempt in range(1, POST_REPLY_MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(
                hermes_url, data=data, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                log.info("posted reply to hermes ctx=%s status=%s", context_id, resp.status)
            return True
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning(
                "failed to POST reply to hermes ctx=%s (attempt %d/%d): %s",
                context_id, attempt, POST_REPLY_MAX_ATTEMPTS, exc,
            )
            if attempt < POST_REPLY_MAX_ATTEMPTS:
                time.sleep(POST_REPLY_BACKOFF_S * attempt)
    log.error(
        "GIVING UP on reply to hermes ctx=%s after %d attempts (last error: %s); "
        "result is LOST", context_id, POST_REPLY_MAX_ATTEMPTS, last_exc,
    )
    return False


# ---------------------------------------------------------------------------
# Inbox processing
# ---------------------------------------------------------------------------

def _read_offset(path: Path) -> int:
    """Read a persisted processed-line offset. Missing/garbage -> 0."""
    try:
        raw = path.read_text().strip()
    except OSError:
        return 0
    try:
        val = int(raw)
    except ValueError:
        return 0
    return val if val >= 0 else 0


def _write_offset(path: Path, offset: int) -> None:
    """Persist the processed-line offset atomically (write tmp + os.replace)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(str(offset))
        os.replace(tmp, path)  # atomic on POSIX
    except OSError as exc:
        log.warning("offset persist failed (%s)", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class Receiver:
    """Owns the inbox poll loop + per-context serialization + reply dispatch."""

    def __init__(
        self,
        cfg: Dict[str, Any],
        runner: Any = None,
        *,
        inbox_path: Path = INBOX_PATH,
        offset_path: Path = INBOX_OFFSET_PATH,
        on_idle_shutdown: Optional[Any] = None,
    ) -> None:
        self.cfg = cfg
        self.runner = runner
        # Resolve the outbound (reply-to-Hermes) bearer once at construction.
        self._hermes_auth_token = resolve_hermes_auth_token(cfg)
        max_ctx = int(cfg.get("max_tracked_contexts") or DEFAULTS["max_tracked_contexts"])
        self.locks = ContextLocks(max_entries=max_ctx)
        self.seen = SeenContexts(max_entries=max_ctx)
        self.inbox_path = inbox_path
        self.offset_path = offset_path
        # Persisted offset => skip the historical backlog on restart (at-most-once).
        self._processed = _read_offset(offset_path)
        self._offset_lock = threading.Lock()
        self._stop = threading.Event()
        # Global cap on concurrent claude subprocesses (per-context lock still
        # serializes same-context turns; this bounds the cross-context fan-out).
        max_turns = int(cfg.get("max_concurrent_turns") or DEFAULTS["max_concurrent_turns"])
        self._turn_slots = threading.BoundedSemaphore(max(1, max_turns))
        # Idle-timeout self-teardown bookkeeping.
        self._last_msg_ts = time.monotonic()
        self._on_idle_shutdown = on_idle_shutdown

    # -- inbox offset -------------------------------------------------------

    def _advance_offset(self, new_offset: int) -> None:
        with self._offset_lock:
            if new_offset <= self._processed:
                return
            self._processed = new_offset
            _write_offset(self.offset_path, new_offset)

    def note_inbound(self) -> None:
        """Record that an inbound message arrived (resets the idle clock)."""
        self._last_msg_ts = time.monotonic()

    # -- turn processing ----------------------------------------------------

    def process_message(self, context_id: str, text: str) -> Optional[str]:
        """Bounded-concurrency + per-context serialization, run turn, POST reply."""
        # Global concurrency cap: bounded wait, then reply [busy] (never block
        # the poll-spawned thread forever).
        wait = float(self.cfg.get("context_lock_wait_s") or DEFAULTS["context_lock_wait_s"])
        if not self._turn_slots.acquire(timeout=wait):
            busy = "[busy] max concurrent turns reached, retry"
            log.warning("ctx=%s busy; concurrency cap reached", context_id)
            _transcript("claude->hermes (busy)", "claude-code", "hermes", context_id, busy)
            post_reply(self.cfg["hermes_url"], context_id, busy, self._hermes_auth_token)
            return busy
        try:
            lock = self.locks.get(context_id)
            acquired = lock.acquire(timeout=wait)
            if not acquired:
                busy = "[busy] this context is processing another turn; retry shortly"
                log.warning("ctx=%s busy; lock wait %.0fs exceeded", context_id, wait)
                _transcript("claude->hermes (busy)", "claude-code", "hermes", context_id, busy)
                post_reply(self.cfg["hermes_url"], context_id, busy, self._hermes_auth_token)
                return busy
            try:
                reply = run_claude_turn(
                    text, context_id, self.cfg, runner=self.runner, seen=self.seen
                )
                out = reply if reply is not None else "[no reply produced by claude]"
                _transcript("claude->hermes", "claude-code", "hermes", context_id, out)
                post_reply(self.cfg["hermes_url"], context_id, out, self._hermes_auth_token)
                return reply
            finally:
                lock.release()
        finally:
            self._turn_slots.release()

    def poll_once(self) -> None:
        """Drain new inbox lines, dispatching each on its own thread (per-context
        locking inside ``process_message`` serializes same-context turns)."""
        if not self.inbox_path.exists():
            return
        # TODO(perf, a2a_fleet v0.4): this rereads the ENTIRE inbox every poll and
        # re-splits O(history) lines just to skip already-processed ones. For a
        # long-lived receiver this is O(n) per poll. Switch to seeking a persisted
        # BYTE offset (read from there to EOF) and/or periodic inbox compaction.
        # Left as a clear TODO deliberately — not half-built here.
        try:
            lines = self.inbox_path.read_text().splitlines()
        except OSError as exc:
            log.warning("inbox read failed (%s)", exc)
            return
        for idx in range(self._processed, len(lines)):
            line = lines[idx].strip()
            # Blank/malformed/non-hermes lines are consumed with no handoff, so
            # persist their offset immediately (at-most-once; nothing to lose).
            if not line:
                self._advance_offset(idx + 1)
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                self._advance_offset(idx + 1)
                continue
            if entry.get("from") != "hermes":
                self._advance_offset(idx + 1)
                continue
            # Missing contextId -> mint a fresh uuid4 (no shared anon sentinel,
            # so unrelated anonymous tasks don't cross-talk on one session/lock).
            context_id = entry.get("contextId") or f"anon-{uuid.uuid4()}"
            text = entry.get("text", "")
            self.note_inbound()
            thread = threading.Thread(
                target=self.process_message,
                args=(context_id, text),
                name=f"turn-{context_id[:12]}",
                daemon=True,
            )
            try:
                thread.start()
            except RuntimeError as exc:
                # Thread creation failed (e.g. resource exhaustion): do NOT advance
                # the offset, so the next poll retries this message rather than
                # losing an already-ACKed task. Stop draining this pass.
                log.warning("failed to start turn thread for ctx=%s (%s); "
                            "leaving message unconsumed for retry", context_id, exc)
                return
            # At-most-once: advance ONLY after a successful handoff. A crash in the
            # window between .start() and here is near-zero; we accept it rather
            # than risk at-least-once re-execution under bypassPermissions.
            self._advance_offset(idx + 1)

    def poll_loop(self) -> None:
        interval = float(self.cfg.get("poll_interval_s") or DEFAULTS["poll_interval_s"])
        log.info("inbox poll loop started (%.1fs)", interval)
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(interval)

    # -- idle-timeout self-teardown ----------------------------------------

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_msg_ts

    def idle_monitor_once(self) -> bool:
        """Return True (and trigger teardown) if idle past the configured limit."""
        idle_to = float(self.cfg.get("idle_timeout_s") or 0)
        if idle_to <= 0:
            return False
        if self.idle_seconds() >= idle_to:
            log.info("idle for %.0fs (>= idle_timeout_s=%.0f); self-teardown",
                     self.idle_seconds(), idle_to)
            self.stop()
            if self._on_idle_shutdown is not None:
                try:
                    self._on_idle_shutdown()
                except Exception as exc:  # noqa: BLE001
                    log.warning("idle shutdown hook failed (%s)", exc)
            return True
        return False

    def idle_monitor_loop(self) -> None:
        idle_to = float(self.cfg.get("idle_timeout_s") or 0)
        if idle_to <= 0:
            return
        # Check on a fraction of the timeout so teardown is reasonably prompt.
        tick = max(1.0, min(idle_to / 4.0, 60.0))
        log.info("idle monitor started (idle_timeout_s=%.0f, tick=%.0fs)", idle_to, tick)
        while not self._stop.is_set():
            if self.idle_monitor_once():
                return
            self._stop.wait(tick)

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

def _agent_card(cfg: Dict[str, Any]) -> Dict[str, Any]:
    base = f"http://{cfg['bind_host']}:{cfg['bind_port']}"
    return {
        "name": "claude-code",
        "description": "Claude Code repo-scoped A2A executor peer (cc_receiver).",
        "url": f"{base}/jsonrpc",
        "version": "0.3.0",
        "protocolVersion": "1.0",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text", "text/plain"],
        "defaultOutputModes": ["text", "text/plain"],
        "securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer"},
        },
        "security": [{"bearerAuth": []}],
        "skills": [
            {
                "id": "execute",
                "name": "Repo executor",
                "description": "Executes A2A tasks in the bound repo via claude -p with full harness.",
                "tags": ["v0.3", "claude_code", "executor"],
            }
        ],
    }


def make_handler(
    cfg: Dict[str, Any],
    expected_token: Optional[str],
    receiver: Optional["Receiver"] = None,
) -> type:
    """Build a BaseHTTPRequestHandler subclass closed over config + token.

    ``receiver`` (when supplied) is notified of inbound messages so the idle
    monitor's clock resets on real traffic.
    """

    def extract_text(params: Dict[str, Any]) -> str:
        message = params.get("message") or {}
        for part in message.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
        return ""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # silence default stderr logging
            pass

        def _json(self, code: int, obj: Dict[str, Any]) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_auth(self) -> bool:
            """True if authorized (or no auth configured). Sends 401 if not."""
            if not expected_token:
                return True  # open (loopback dev); warning logged at startup.
            header = self.headers.get("Authorization", "")
            if not header.lower().startswith("bearer "):
                self._json(401, {"error": "missing bearer token"})
                return False
            # "bearer " prefix matched, but the token may be missing/whitespace:
            # split can yield a single element -> guard against IndexError (500).
            parts = header.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                self._json(401, {"error": "missing bearer token"})
                return False
            presented = parts[1].strip()
            if not hmac.compare_digest(presented.encode(), expected_token.encode()):
                self._json(401, {"error": "invalid bearer token"})
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path == "/health":
                self._json(200, {
                    "ok": True,
                    "name": "claude-code",
                    "repo_path": cfg["repo_path"],
                })
            elif self.path.startswith("/.well-known/agent-card.json"):
                self._json(200, _agent_card(cfg))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if self.path != "/jsonrpc":
                self._json(404, {"error": "not found"})
                return
            if not self._check_auth():
                return
            # Parse Content-Length defensively: malformed -> -32600, oversized ->
            # HTTP 413 BEFORE allocating the read buffer (DoS guard).
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except (TypeError, ValueError):
                self._json(200, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600, "message": "invalid Content-Length"}})
                return
            if length < 0:
                self._json(200, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600, "message": "invalid Content-Length"}})
                return
            if length > MAX_BODY_BYTES:
                body = json.dumps({"error": "request entity too large"}).encode()
                self.send_response(413)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            try:
                body = json.loads(self.rfile.read(length).decode())
            except (json.JSONDecodeError, ValueError):
                self._json(200, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "parse error"}})
                return
            if not isinstance(body, dict):
                self._json(200, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600, "message": "invalid request"}})
                return
            rpc_id = body.get("id")
            method = body.get("method")
            params = body.get("params") or {}
            if method not in ("SendMessage", "message/send"):
                self._json(200, {"jsonrpc": "2.0", "id": rpc_id,
                                 "error": {"code": -32601, "message": f"method not found: {method!r}"}})
                return
            text = extract_text(params)
            # Clamp prompt size before it can reach claude.
            if len(text) > MAX_PROMPT_CHARS:
                text = text[:MAX_PROMPT_CHARS]
            if "contextId" in params:
                self._json(200, {"jsonrpc": "2.0", "id": rpc_id,
                                 "error": {"code": -32602,
                                           "message": "contextId must be nested under params.message, "
                                                      "not at params root (A2A spec)"}})
                return
            message = params.get("message") or {}
            # Missing contextId -> fresh uuid4 (no shared anon sentinel).
            context_id = message.get("contextId") or f"anon-{uuid.uuid4()}"
            # Queue to inbox; the poll loop processes asynchronously. The append
            # is serialized via _INBOX_LOCK so concurrent POSTs can't tear lines.
            try:
                _append_jsonl(INBOX_PATH, {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "from": "hermes",
                    "contextId": context_id,
                    "text": text,
                }, _INBOX_LOCK)
            except OSError as exc:
                self._json(200, {"jsonrpc": "2.0", "id": rpc_id,
                                 "error": {"code": -32000, "message": f"inbox write failed: {exc}"}})
                return
            if receiver is not None:
                receiver.note_inbound()
            _transcript("hermes->claude", "hermes", "claude-code", context_id, text)
            ack = "Message received; executing in repo via Claude Code. Reply will follow. [queued]"
            _transcript("claude->hermes (ack)", "claude-code", "hermes", context_id, ack)
            self._json(200, {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "kind": "message",
                    "message": {"role": "agent", "parts": [{"text": ack}], "contextId": context_id},
                },
            })

    return Handler


# ---------------------------------------------------------------------------
# Harness-load visibility
# ---------------------------------------------------------------------------

def log_harness_inventory(repo_path: Path) -> Dict[str, bool]:
    """Log + return which harness assets exist in the repo (visibility)."""
    inventory = {
        "CLAUDE.md": (repo_path / "CLAUDE.md").exists(),
        ".mcp.json": (repo_path / ".mcp.json").exists(),
        ".claude": (repo_path / ".claude").exists(),
    }
    log.info(
        "harness inventory for %s: CLAUDE.md=%s .mcp.json=%s .claude=%s",
        repo_path, inventory["CLAUDE.md"], inventory[".mcp.json"], inventory[".claude"],
    )
    return inventory


# ---------------------------------------------------------------------------
# PID file
# ---------------------------------------------------------------------------

def is_loopback_bind(host: str) -> bool:
    """True if ``host`` is a loopback address (auth may be optional there)."""
    return str(host).strip().lower() in {"127.0.0.1", "::1", "localhost", ""}


def probe_claude_cli() -> bool:
    """Best-effort ``claude --version`` probe; loud warning if missing. Non-fatal."""
    try:
        proc = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            log.info("claude CLI present: %s", (proc.stdout or "").strip()[:120])
            return True
        log.warning("claude --version exited rc=%s: %s", proc.returncode,
                    (proc.stderr or "").strip()[:200])
        return False
    except FileNotFoundError:
        log.warning("claude CLI NOT FOUND on PATH — turns will fail fatally "
                    "with '[error] claude CLI not found on PATH'")
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("claude --version probe failed (%s)", exc)
        return False


def write_pid_file(path: Optional[Path] = None) -> None:
    # Resolve PID_PATH at CALL time (not as a default-arg binding) so the
    # module-level constant is authoritative — and so tests can monkeypatch it.
    if path is None:
        path = PID_PATH
    try:
        path.write_text(str(os.getpid()))
    except OSError as exc:
        log.warning("could not write PID file %s (%s)", path, exc)


def remove_pid_file(path: Optional[Path] = None) -> None:
    if path is None:
        path = PID_PATH
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not remove PID file %s (%s)", path, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [cc_receiver] %(levelname)s %(message)s",
    )
    cfg = load_config()
    repo_path = Path(cfg["repo_path"])

    expected_token = resolve_auth_token(cfg)
    bind_host = cfg.get("bind_host", "")

    # Fail-closed: a non-loopback bind with no auth token is an open RCE surface
    # (bypassPermissions). Refuse to start rather than merely warn.
    if not expected_token and not is_loopback_bind(bind_host):
        log.error(
            "refusing to start: bind_host=%r is not loopback and no auth token is "
            "configured (auth_token_env=%r). Set an auth token or bind to loopback.",
            bind_host, cfg.get("auth_token_env"),
        )
        return 2

    INBOX_PATH.touch(exist_ok=True)
    TRANSCRIPT_PATH.touch(exist_ok=True)

    if not expected_token:
        log.warning(
            "no bearer token configured (auth_token_env=%r) — POST /jsonrpc is OPEN. "
            "Acceptable only on a loopback dev bind.",
            cfg.get("auth_token_env"),
        )

    probe_claude_cli()
    log_harness_inventory(repo_path)
    log.info("repo_path (cwd for claude) pinned to %s", repo_path)

    httpd_box: Dict[str, Any] = {}

    def _idle_teardown() -> None:
        log.info("idle-timeout teardown: removing PID file and stopping server")
        remove_pid_file()
        httpd = httpd_box.get("httpd")
        if httpd is not None:
            threading.Thread(target=httpd.shutdown, name="idle-shutdown", daemon=True).start()

    receiver = Receiver(cfg, on_idle_shutdown=_idle_teardown)

    # Bind the server FIRST. A failed bind (port in use) must NOT leave a stale
    # PID file behind to poison status/stop — so the pidfile is written only
    # AFTER a successful bind, and removed if bind raises.
    handler = make_handler(cfg, expected_token, receiver)
    try:
        httpd = ThreadingHTTPServer((cfg["bind_host"], int(cfg["bind_port"])), handler)
    except OSError as exc:
        log.error("failed to bind %s:%s (%s); not writing PID file",
                  cfg["bind_host"], cfg["bind_port"], exc)
        remove_pid_file()  # defensive: ensure no stale pidfile survives a bind failure
        return 2
    httpd_box["httpd"] = httpd
    write_pid_file()

    poll_thread = threading.Thread(target=receiver.poll_loop, name="inbox-poll", daemon=True)
    poll_thread.start()

    if float(cfg.get("idle_timeout_s") or 0) > 0:
        idle_thread = threading.Thread(
            target=receiver.idle_monitor_loop, name="idle-monitor", daemon=True
        )
        idle_thread.start()

    def _shutdown(signum: int, _frame: Any) -> None:
        log.info("signal %s received; shutting down", signum)
        receiver.stop()
        httpd.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("cc_receiver listening on http://%s:%s", cfg["bind_host"], cfg["bind_port"])
    try:
        httpd.serve_forever()
    finally:
        receiver.stop()
        remove_pid_file()
        log.info("cc_receiver stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
