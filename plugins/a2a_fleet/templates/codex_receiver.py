#!/usr/bin/env python3
"""Standalone A2A receiver — OpenAI Codex CLI as a repo-scoped executor peer.

This file is a TEMPLATE. Hermes' ``deploy_codex_receiver`` tool copies it
verbatim into a target repo's ``<repo>/.hermes/codex_receiver.py`` and writes a
sibling ``codex_receiver.json`` config. The receiver then runs as a detached
daemon that:

  1. Serves an A2A surface on ``bind_host:bind_port``
     (GET /health, GET /.well-known/agent-card.json, POST /jsonrpc).
  2. Queues inbound messages to an inbox JSONL and ACKs immediately.
  3. A background poll loop drains the inbox, spawning ``codex exec`` with the
     repo as cwd (pinned from config, NEVER from inbound message).
  4. Maintains a persistent Codex thread per A2A ``contextId`` via
     a durable ``a2a-codex-sessions.json`` map + ``codex exec resume <thread_id>``.
  5. POSTs the result back to ``hermes_url`` as a JSON-RPC SendMessage.

Design constraints (deliberate):
  * STDLIB ONLY (+ the ``codex`` CLI). No import of the a2a_fleet package, no
    Hermes gateway dependency — it must run on its own inside any repo.
  * ``cwd`` for codex is ALWAYS ``repo_path`` from config, NEVER taken from an
    inbound message (a remote peer must not be able to redirect execution).
  * Per-contextId serialization: two concurrent turns on the same contextId must
    NOT both mint / reuse the same Codex thread concurrently. A per-contextId
    lock serializes same-context turns; different contextIds run concurrently.

Subprocess CLI contract (codex-cli 0.135.0):
  First turn (no stored thread_id):
    codex exec "<prompt>" --json --skip-git-repo-check -s <sandbox> [-m <model>]
  Resume turn (stored thread_id exists):
    codex exec resume <thread_id> "<prompt>" --json --skip-git-repo-check [-m <model>]
  * Do NOT pass --color (asymmetry: accepted on exec but REJECTED on exec resume).
  * Do NOT pass --ephemeral (disables resume).
  * Sandbox default: workspace-write  (read-only | workspace-write | danger-full-access)
  * -s/--sandbox only on first turn; resume inherits the sandbox from thread creation.

JSONL output parsing:
  Events are line-delimited JSON on STDOUT. Key event types:
    {"type":"thread.started","thread_id":"<uuid>"}
    {"type":"turn.started"}
    {"type":"item.completed","item":{"id":"...","type":"agent_message","text":"<reply>"}}
    {"type":"turn.completed","usage":{...}}
  * RESUMABLE SESSION ID: thread_id from "thread.started" event.
  * FINAL REPLY: the LAST item.completed event where item["type"]=="agent_message" -> item["text"].
  * SESSION-NOT-FOUND: "no rollout found for thread id" in reply text or stderr.

Reply contract (receiver -> Hermes), POSTed to ``hermes_url``::

    {"jsonrpc": "2.0", "id": "codex-<ts>", "method": "SendMessage",
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
CONFIG_PATH = SCRIPT_DIR / "codex_receiver.json"
INBOX_PATH = SCRIPT_DIR / "a2a-codex-inbox.jsonl"
INBOX_OFFSET_PATH = SCRIPT_DIR / "a2a-codex-inbox.offset"
TRANSCRIPT_PATH = SCRIPT_DIR / "a2a-codex-transcript.jsonl"
PID_PATH = SCRIPT_DIR / "codex_receiver.pid"

# Cap on a single inbound JSON-RPC body (DoS guard) and the prompt we hand to
# codex. 1 MiB body is generous for text tasks; oversized bodies are rejected
# with HTTP 413 before allocation.
MAX_BODY_BYTES = 1 * 1024 * 1024
MAX_PROMPT_CHARS = 256 * 1024
# Cap codex stdout we buffer in memory (defensive — runaway tool output).
MAX_STDOUT_BYTES = 8 * 1024 * 1024
TOKEN_PATH = SCRIPT_DIR / ".codex-token"
SESSION_MAP_PATH = SCRIPT_DIR / "a2a-codex-sessions.json"

# A signal in a result frame / stderr that a Codex thread genuinely does not
# exist (the ONLY condition under which we retry with a fresh first turn — see
# ``run_codex_turn``). Kept narrow on purpose: this receiver runs autonomously
# with ``--skip-git-repo-check``, so a spurious retry can double-execute a
# side-effecting turn.
SESSION_NOT_FOUND_SIGNALS = (
    "no rollout found for thread id",
)

DEFAULT_SANDBOX = "workspace-write"

DEFAULTS: Dict[str, Any] = {
    "repo_path": str(SCRIPT_DIR.parent),  # .hermes/ is inside the repo
    "bind_host": "127.0.0.1",
    "bind_port": 9311,
    "hermes_url": "http://127.0.0.1:9219/jsonrpc",
    "role_prompt": (
        "You are a Codex CLI executor peer in an A2A fleet. The orchestrator "
        "is Hermes. You receive tasks over A2A and execute them in THIS repo "
        "using your full tools/skills. Reply concisely with results/status. "
        "Same contextId = same ongoing session/thread."
    ),
    "role_file": None,            # if set, read role prompt from this path (overrides role_prompt)
    "codex_model": None,
    "codex_sandbox": DEFAULT_SANDBOX,
    "codex_extra_flags": [],      # list[str] appended verbatim to the command
    "auth_token_env": None,       # env var name holding the INBOUND bearer token (POST /jsonrpc)
    "hermes_auth_token_env": None,  # env var name holding the bearer token for OUTBOUND replies to Hermes
    "poll_interval_s": 2.0,
    "codex_timeout_s": 300,
    "context_lock_wait_s": 600.0,  # how long a queued same-context turn waits for the lock
    "max_concurrent_turns": 3,     # global cap on simultaneous Codex subprocesses
    "max_tracked_contexts": 1024,  # bound on the per-context lock registry
    "idle_timeout_s": 1800,        # self-teardown after this many idle seconds (0 = disabled)
}

log = logging.getLogger("codex_receiver")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(config_path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """Read ``codex_receiver.json`` (sibling of this script), merge over DEFAULTS.

    Missing / malformed config is non-fatal: defaults are used and a warning is
    logged. ``role_file`` (if set + readable) supplies the role prompt.
    """
    cfg = dict(DEFAULTS)
    cfg["codex_extra_flags"] = list(DEFAULTS["codex_extra_flags"])
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

    if not isinstance(cfg.get("codex_extra_flags"), list):
        log.warning("codex_extra_flags is not a list; ignoring")
        cfg["codex_extra_flags"] = []

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
    unset/empty value -> None (no Authorization header sent).
    """
    env_name = cfg.get("hermes_auth_token_env")
    if not env_name:
        return None
    token = os.environ.get(env_name)
    return token or None


# ---------------------------------------------------------------------------
# Durable Codex thread map (contextId -> thread_id)
# ---------------------------------------------------------------------------

_SESSION_MAP_LOCK = threading.Lock()


def load_session_map(path: Path = SESSION_MAP_PATH) -> Dict[str, Dict[str, Any]]:
    """Load the durable Codex thread map. Malformed content -> empty map."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("session map %s unreadable (%s); treating as empty", path, exc)
        return {}
    if not isinstance(raw, dict):
        log.warning("session map %s is not a JSON object; treating as empty", path)
        return {}
    clean: Dict[str, Dict[str, Any]] = {}
    for context_id, entry in raw.items():
        if not isinstance(context_id, str) or not isinstance(entry, dict):
            continue
        thread_id = entry.get("thread_id")
        updated_at = entry.get("updated_at")
        if isinstance(thread_id, str) and thread_id.strip():
            clean[context_id] = {
                "thread_id": thread_id.strip(),
                "updated_at": int(updated_at) if isinstance(updated_at, (int, float)) else int(time.time()),
            }
    return clean


def get_thread_id_for_context(context_id: str, path: Path = SESSION_MAP_PATH) -> Optional[str]:
    entry = load_session_map(path).get(context_id) or {}
    thread_id = entry.get("thread_id")
    if isinstance(thread_id, str) and thread_id.strip():
        return thread_id.strip()
    return None


def store_thread_id_for_context(context_id: str, thread_id: str, path: Path = SESSION_MAP_PATH) -> None:
    if not thread_id.strip():
        return
    with _SESSION_MAP_LOCK:
        data = load_session_map(path)
        data[context_id] = {
            "thread_id": thread_id.strip(),
            "updated_at": int(time.time()),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("session map persist failed (%s)", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def clear_thread_id_for_context(context_id: str, path: Path = SESSION_MAP_PATH) -> None:
    """Remove a stale/dead thread_id entry for ``context_id`` from the session map.

    Atomic tmp+os.replace write, same as ``store_thread_id_for_context``.
    Called under the per-contextId lock before a remint retry so that a failed
    remint leaves the map clean (no stale id re-persisted on the next turn).
    Must be called while already holding _SESSION_MAP_LOCK or before acquiring it
    — this function acquires the lock itself.
    """
    with _SESSION_MAP_LOCK:
        data = load_session_map(path)
        if context_id not in data:
            return
        del data[context_id]
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("session map clear failed for ctx=%s (%s)", context_id, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------


def _prompt_with_role(prompt: str, cfg: Dict[str, Any]) -> str:
    role = str(cfg.get("role_prompt") or DEFAULTS["role_prompt"]).strip()
    prompt = prompt.strip()
    if not role:
        return prompt
    if not prompt:
        return role
    return role + "\n\n" + prompt


def build_codex_command(
    prompt: str,
    cfg: Dict[str, Any],
    *,
    thread_id: Optional[str] = None,
) -> List[str]:
    """Build the ``codex exec`` argv for one turn.

    First turn (no thread_id):
        codex exec "<prompt>" --json --skip-git-repo-check -s <sandbox> [-m <model>]
    Resume turn (thread_id stored):
        codex exec resume <thread_id> "<prompt>" --json --skip-git-repo-check [-m <model>]

    IMPORTANT:
      * No --color: asymmetry between exec (accepts) and exec resume (rejects).
      * No --ephemeral: disables resume capability.
      * -s/--sandbox only on first turn; resume inherits sandbox from thread creation.
    """
    full_prompt = _prompt_with_role(prompt, cfg)
    if thread_id:
        # Resume turn: codex exec resume <thread_id> "<prompt>" --json --skip-git-repo-check
        cmd: List[str] = ["codex", "exec", "resume", thread_id, full_prompt]
    else:
        # First turn: codex exec "<prompt>" --json --skip-git-repo-check -s <sandbox>
        sandbox = str(cfg.get("codex_sandbox") or DEFAULT_SANDBOX)
        cmd = ["codex", "exec", full_prompt, "-s", sandbox]
    cmd += ["--json", "--skip-git-repo-check"]
    model = cfg.get("codex_model")
    if model:
        cmd += ["-m", str(model)]
    extra = cfg.get("codex_extra_flags") or []
    if isinstance(extra, list):
        cmd += _sanitize_extra_flags(extra, is_resume=bool(thread_id))
    return cmd


# Flags that are ALWAYS forbidden (they break the resume model wholesale).
_FORBIDDEN_ANY: frozenset[str] = frozenset({"--ephemeral"})
# Flags that are forbidden only on resume (rejected by codex exec resume).
_FORBIDDEN_RESUME: frozenset[str] = frozenset({"--color", "-s", "--sandbox"})


def _sanitize_extra_flags(extra: List[str], *, is_resume: bool) -> List[str]:
    """Return a copy of ``extra`` with forbidden flags (and their values) removed.

    Handles both ``--flag value`` (two tokens) and ``--flag=value`` (one token).
    Forbidden on ANY command: ``--ephemeral``.
    Forbidden on RESUME only: ``--color``, ``-s``/``--sandbox``.
    Logs a warning for each dropped flag so operators notice stale configs.
    """
    forbidden = set(_FORBIDDEN_ANY)
    if is_resume:
        forbidden |= _FORBIDDEN_RESUME

    result: List[str] = []
    tokens = [str(x) for x in extra]
    i = 0
    while i < len(tokens):
        tok = tokens[i]

        # --flag=value form: extract the flag portion before '='
        base = tok.split("=", 1)[0] if "=" in tok else tok

        if base in forbidden:
            log.warning(
                "dropping forbidden codex_extra_flags token %r%s",
                tok,
                " (resume command)" if is_resume else "",
            )
            i += 1
            # --flag value form (no '='): also consume the following value token
            # if it does not look like a flag itself.
            if "=" not in tok and i < len(tokens) and not tokens[i].startswith("-"):
                log.warning(
                    "dropping forbidden codex_extra_flags value token %r%s",
                    tokens[i],
                    " (resume command)" if is_resume else "",
                )
                i += 1
            continue

        result.append(tok)
        i += 1

    return result


# ---------------------------------------------------------------------------
# Deterministic result parsing
# ---------------------------------------------------------------------------


def parse_codex_output(stdout: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse Codex JSONL stdout into thread_id and reply text.

    Event format (codex-cli 0.135.0):
      {"type":"thread.started","thread_id":"<uuid>"}
      {"type":"turn.started"}
      {"type":"item.completed","item":{"id":"...","type":"agent_message","text":"<reply>"}}
      {"type":"turn.completed","usage":{...}}

    Returns (thread_id, reply_text):
      * thread_id: from the first "thread.started" event -> event["thread_id"]
      * reply_text: the LAST item.completed where item["type"]=="agent_message" -> item["text"]
        (there may be multiple item.completed events for tool calls/file changes)
    """
    thread_id: Optional[str] = None
    last_agent_reply: Optional[str] = None

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

        event_type = obj.get("type")

        # Capture thread_id from thread.started event
        if event_type == "thread.started" and thread_id is None:
            tid = obj.get("thread_id")
            if isinstance(tid, str) and tid.strip():
                thread_id = tid.strip()

        # Capture the LAST agent_message from item.completed events
        if event_type == "item.completed":
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    last_agent_reply = text

    return thread_id, last_agent_reply or None


# ---------------------------------------------------------------------------
# Per-contextId serialization
# ---------------------------------------------------------------------------


class ContextLocks:
    """Hand out one ``threading.Lock`` per contextId, with bounded eviction.

    Same contextId -> same lock (so its turns serialize). Different contextIds
    -> different locks (so they run concurrently). A registry mutex guards lock
    creation / eviction only; it is NEVER held while a turn runs.

    The registry is bounded to ``max_entries`` via LRU eviction. A lock is only
    evicted if it is NOT currently held (``lock.locked()`` is False). Held locks
    are never evicted, so the registry may temporarily exceed the bound rather
    than corrupt same-context serialization.
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
            self._registry.move_to_end(context_id)
            self._evict_locked()
            return lock

    def _evict_locked(self) -> None:
        if len(self._registry) <= self._max_entries:
            return
        overflow = len(self._registry) - self._max_entries
        evicted = 0
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

# The runner contract is ``(cmd, cwd, timeout) -> (stdout, rc, stderr)``.
# (stderr is appended so the poll loop can surface a snippet on failure;
# legacy 2-tuple runners are still accepted for backward compatibility.)


class CodexCLINotFound(Exception):
    """Raised by the runner when the ``codex`` binary is not on PATH."""


def run_codex_turn(
    prompt: str,
    context_id: str,
    cfg: Dict[str, Any],
    *,
    runner: Any = None,
) -> Optional[str]:
    """Run one Codex turn for ``context_id`` with narrow thread remint."""
    if runner is None:
        runner = _subprocess_runner

    # Look up SESSION_MAP_PATH dynamically (module-level variable may be
    # monkeypatched in tests; default-argument form captures the original value).
    session_map_path: Path = SESSION_MAP_PATH

    repo_path = Path(cfg["repo_path"])
    timeout = float(cfg.get("codex_timeout_s") or DEFAULTS["codex_timeout_s"])
    stored_thread_id = get_thread_id_for_context(context_id, session_map_path)

    def _invoke(thread_id: Optional[str]) -> Tuple[Optional[str], Optional[str], int, str]:
        cmd = build_codex_command(prompt, cfg, thread_id=thread_id)
        stdout, rc, stderr = _call_runner(runner, cmd, str(repo_path), timeout)
        parsed_thread_id, reply = parse_codex_output(stdout)
        if parsed_thread_id:
            store_thread_id_for_context(context_id, parsed_thread_id, session_map_path)
        return parsed_thread_id, reply, rc, stderr

    try:
        parsed_thread_id, reply, rc, stderr = _invoke(stored_thread_id)
    except subprocess.TimeoutExpired:
        return f"[error] codex turn timed out after {timeout}s"
    except (FileNotFoundError, CodexCLINotFound):
        return "[error] codex CLI not found on PATH"
    except Exception as exc:  # noqa: BLE001
        log.warning("codex invocation failed (%s)", exc)
        return f"[error] codex invocation failed: {exc}"

    # Remint: only on genuine session-not-found signal in BOTH reply and stderr
    if stored_thread_id and _is_session_not_found(reply, stderr):
        log.info("stored thread %s missing for ctx=%s; reminting new Codex thread", stored_thread_id, context_id)
        # Clear the stale thread_id BEFORE the retry so that if the retry also
        # fails (emits no thread.started), the bad id is not left on disk and
        # the next turn does not blindly attempt resume <dead-id> again.
        clear_thread_id_for_context(context_id, session_map_path)
        try:
            parsed_thread_id, reply, rc, stderr = _invoke(None)
        except subprocess.TimeoutExpired:
            return f"[error] codex turn timed out after {timeout}s"
        except (FileNotFoundError, CodexCLINotFound):
            return "[error] codex CLI not found on PATH"
        except Exception as exc:  # noqa: BLE001
            log.warning("codex remint failed (%s)", exc)
            return f"[error] codex invocation failed: {exc}"
        if parsed_thread_id:
            log.info("reminted Codex thread %s for ctx=%s", parsed_thread_id, context_id)

    if reply is None and rc != 0:
        snippet = (stderr or "").strip().replace("\n", " ")[:300]
        reply = (
            f"[error] codex exited rc={rc} with no parseable output"
            + (f": {snippet}" if snippet else "")
        )
    return reply


def _call_runner(runner: Any, cmd: List[str], cwd: str, timeout: float) -> Tuple[str, int, str]:
    """Invoke a runner that may return a 2-tuple (legacy) or 3-tuple (with stderr)."""
    result = runner(cmd, cwd, timeout)
    if isinstance(result, tuple) and len(result) == 3:
        stdout, rc, stderr = result
        return stdout, rc, (stderr or "")
    stdout, rc = result  # type: ignore[misc]
    return stdout, rc, ""


def _is_session_not_found(reply: Optional[str], stderr: str) -> bool:
    """True only on a genuine Codex thread-not-found signal (reply text or stderr).

    Deliberately narrow: we do NOT treat a bare non-zero rc or a None reply as a
    session error, because retrying would re-run the turn.
    Checks BOTH reply text AND stderr (JSON-RPC error -32600 may surface in either).
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


# Common tool dirs appended to PATH for the spawned codex process. A receiver
# launched by launchd (or any non-login daemon) inherits a minimal PATH, so
# codex's tool/command_execution calls can't find `gh`/`git`/node. We APPEND
# (never shadow) so an explicit parent PATH still wins; only missing dirs are
# added as fallbacks.
_EXTRA_PATH_DIRS: Tuple[str, ...] = (
    "/opt/homebrew/bin", "/opt/homebrew/sbin",
    "/usr/local/bin", "/usr/local/sbin",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
)


def _tool_env() -> Dict[str, str]:
    """os.environ copy with common tool dirs appended to PATH (gh/git/node)."""
    env = dict(os.environ)
    parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    for d in (os.path.expanduser("~/.local/bin"), *_EXTRA_PATH_DIRS):
        if d and d not in parts and os.path.isdir(d):
            parts.append(d)
    env["PATH"] = os.pathsep.join(parts)
    return env


def _subprocess_runner(cmd: List[str], cwd: str, timeout: float) -> Tuple[str, int, str]:
    """Real subprocess invocation of ``codex exec``.

    Uses ``Popen`` + ``start_new_session=True`` so the whole process tree lands in
    its own process group; on timeout we ``killpg`` the group to reap orphans.
    Returns (stdout, rc, stderr). Buffers are capped defensively.

    stdin=DEVNULL is REQUIRED (codex-cli >= 0.136). codex exec inspects whether
    stdin is a pipe; if it is (which a detached daemon's inherited stdin is) and
    the positional prompt isn't consumed, codex blocks "Reading additional input
    from stdin..." and exits rc=1 with no parseable output (issue #97). Closing
    stdin forces codex to use the positional prompt argument. The prompt is ALWAYS
    passed as a positional arg (see build_codex_command), never via stdin.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=_tool_env(),
        )
    except FileNotFoundError:
        raise CodexCLINotFound("codex") from None
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
        log.warning("codex stderr: %s", stderr.strip()[:500])
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
    is sent. Retries up to ``POST_REPLY_MAX_ATTEMPTS`` with short backoff.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": f"codex-{int(time.time() * 1000)}",
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
        self.inbox_path = inbox_path
        self.offset_path = offset_path
        # Persisted offset => skip the historical backlog on restart (at-most-once).
        self._processed = _read_offset(offset_path)
        self._offset_lock = threading.Lock()
        self._stop = threading.Event()
        # Global cap on concurrent Codex subprocesses (per-context lock still
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
        wait = float(self.cfg.get("context_lock_wait_s") or DEFAULTS["context_lock_wait_s"])
        if not self._turn_slots.acquire(timeout=wait):
            busy = "[busy] max concurrent turns reached, retry"
            log.warning("ctx=%s busy; concurrency cap reached", context_id)
            _transcript("codex->hermes (busy)", "codex", "hermes", context_id, busy)
            post_reply(self.cfg["hermes_url"], context_id, busy, self._hermes_auth_token)
            return busy
        try:
            lock = self.locks.get(context_id)
            acquired = lock.acquire(timeout=wait)
            if not acquired:
                busy = "[busy] this context is processing another turn; retry shortly"
                log.warning("ctx=%s busy; lock wait %.0fs exceeded", context_id, wait)
                _transcript("codex->hermes (busy)", "codex", "hermes", context_id, busy)
                post_reply(self.cfg["hermes_url"], context_id, busy, self._hermes_auth_token)
                return busy
            try:
                reply = run_codex_turn(
                    text, context_id, self.cfg, runner=self.runner
                )
                out = reply if reply is not None else "[no reply produced by codex]"
                _transcript("codex->hermes", "codex", "hermes", context_id, out)
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
        try:
            lines = self.inbox_path.read_text().splitlines()
        except OSError as exc:
            log.warning("inbox read failed (%s)", exc)
            return
        for idx in range(self._processed, len(lines)):
            line = lines[idx].strip()
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
            # Missing contextId -> mint a fresh uuid4
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
                log.warning("failed to start turn thread for ctx=%s (%s); "
                            "leaving message unconsumed for retry", context_id, exc)
                return
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
        "name": "codex",
        "description": "Codex CLI repo-scoped A2A executor peer (codex_receiver).",
        "url": f"{base}/jsonrpc",
        "version": "0.1.0",
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
                "description": "Executes A2A tasks in the bound repo via codex exec with full harness.",
                "tags": ["v0.1", "codex", "executor"],
            }
        ],
    }


def make_handler(
    cfg: Dict[str, Any],
    expected_token: Optional[str],
    receiver: Optional["Receiver"] = None,
) -> type:
    """Build a BaseHTTPRequestHandler subclass closed over config + token."""

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
            parts = header.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                self._json(401, {"error": "missing bearer token"})
                return False
            presented = parts[1].strip()
            if not hmac.compare_digest(presented.encode(), expected_token.encode()):
                self._json(401, {"error": "invalid bearer token"})
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._json(200, {
                    "ok": True,
                    "name": "codex",
                    "repo_path": cfg["repo_path"],
                })
            elif self.path.startswith("/.well-known/agent-card.json"):
                self._json(200, _agent_card(cfg))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/jsonrpc":
                self._json(404, {"error": "not found"})
                return
            if not self._check_auth():
                return
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
            if len(text) > MAX_PROMPT_CHARS:
                text = text[:MAX_PROMPT_CHARS]
            message = params.get("message") or {}
            context_id = message.get("contextId") or f"anon-{uuid.uuid4()}"
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
            _transcript("hermes->codex", "hermes", "codex", context_id, text)
            ack = "Message received; executing in repo via Codex. Reply will follow. [queued]"
            _transcript("codex->hermes (ack)", "codex", "hermes", context_id, ack)
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
        "AGENTS.md": (repo_path / "AGENTS.md").exists(),
        ".mcp.json": (repo_path / ".mcp.json").exists(),
        ".codex": (repo_path / ".codex").exists(),
    }
    log.info(
        "harness inventory for %s: AGENTS.md=%s .mcp.json=%s .codex=%s",
        repo_path, inventory["AGENTS.md"], inventory[".mcp.json"], inventory[".codex"],
    )
    return inventory


# ---------------------------------------------------------------------------
# PID file
# ---------------------------------------------------------------------------

def is_loopback_bind(host: str) -> bool:
    """True if ``host`` is a loopback address (auth may be optional there)."""
    return str(host).strip().lower() in {"127.0.0.1", "::1", "localhost", ""}


def probe_codex_cli() -> bool:
    """Best-effort ``codex --version`` probe; loud warning if missing. Non-fatal."""
    try:
        proc = subprocess.run(
            ["codex", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            log.info("codex CLI present: %s", (proc.stdout or "").strip()[:120])
            return True
        log.warning("codex --version exited rc=%s: %s", proc.returncode,
                    (proc.stderr or "").strip()[:200])
        return False
    except FileNotFoundError:
        log.warning("codex CLI NOT FOUND on PATH — turns will fail fatally "
                    "with '[error] codex CLI not found on PATH'")
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("codex --version probe failed (%s)", exc)
        return False


def write_pid_file(path: Optional[Path] = None) -> None:
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
        format="%(asctime)s [codex_receiver] %(levelname)s %(message)s",
    )
    cfg = load_config()
    repo_path = Path(cfg["repo_path"])

    expected_token = resolve_auth_token(cfg)
    bind_host = cfg.get("bind_host", "")

    # Fail-closed: a non-loopback bind with no auth token is an open RCE surface.
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

    probe_codex_cli()
    log_harness_inventory(repo_path)
    log.info("repo_path (cwd for codex) pinned to %s", repo_path)

    httpd_box: Dict[str, Any] = {}

    def _idle_teardown() -> None:
        log.info("idle-timeout teardown: removing PID file and stopping server")
        remove_pid_file()
        httpd = httpd_box.get("httpd")
        if httpd is not None:
            threading.Thread(target=httpd.shutdown, name="idle-shutdown", daemon=True).start()

    receiver = Receiver(cfg, on_idle_shutdown=_idle_teardown)

    handler = make_handler(cfg, expected_token, receiver)
    try:
        httpd = ThreadingHTTPServer((cfg["bind_host"], int(cfg["bind_port"])), handler)
    except OSError as exc:
        log.error("failed to bind %s:%s (%s); not writing PID file",
                  cfg["bind_host"], cfg["bind_port"], exc)
        remove_pid_file()
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

    log.info("codex_receiver listening on http://%s:%s", cfg["bind_host"], cfg["bind_port"])
    try:
        httpd.serve_forever()
    finally:
        receiver.stop()
        remove_pid_file()
        log.info("codex_receiver stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
