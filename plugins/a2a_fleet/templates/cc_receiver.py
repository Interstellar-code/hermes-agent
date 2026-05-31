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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "a2a_receiver.json"
INBOX_PATH = SCRIPT_DIR / "a2a-inbox.jsonl"
TRANSCRIPT_PATH = SCRIPT_DIR / "a2a-transcript.jsonl"
PID_PATH = SCRIPT_DIR / "cc_receiver.pid"

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
    "auth_token_env": None,       # env var name holding the bearer token
    "poll_interval_s": 2.0,
    "claude_timeout_s": 300,
    "context_lock_wait_s": 600.0,  # how long a queued same-context turn waits for the lock
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
    """Return the bearer token from ``auth_token_env`` env var, or None."""
    env_name = cfg.get("auth_token_env")
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
        cmd += [str(x) for x in extra]
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
    """Hand out one ``threading.Lock`` per contextId.

    Same contextId -> same lock (so its turns serialize). Different contextIds
    -> different locks (so they run concurrently). A registry mutex guards lock
    creation only; it is never held while a turn runs.
    """

    def __init__(self) -> None:
        self._registry: Dict[str, threading.Lock] = {}
        self._mutex = threading.Lock()

    def get(self, context_id: str) -> threading.Lock:
        with self._mutex:
            lock = self._registry.get(context_id)
            if lock is None:
                lock = threading.Lock()
                self._registry[context_id] = lock
            return lock


# ---------------------------------------------------------------------------
# Turn execution
# ---------------------------------------------------------------------------

# Tracks which contextIds already started a session (so we know resume vs first).
_seen_contexts: set[str] = set()
_seen_mutex = threading.Lock()


def _has_seen(context_id: str) -> bool:
    with _seen_mutex:
        return context_id in _seen_contexts


def _mark_seen(context_id: str) -> None:
    with _seen_mutex:
        _seen_contexts.add(context_id)


def run_claude_turn(
    prompt: str,
    context_id: str,
    cfg: Dict[str, Any],
    *,
    runner: Any = None,
) -> Optional[str]:
    """Run one claude turn for ``context_id`` with self-correcting session mode.

    ``runner`` is an injectable callable ``(cmd, cwd, timeout) -> (stdout, rc)``
    used by tests to stub the subprocess. Defaults to the real subprocess call.

    Session strategy: try resume-vs-first based on whether we've seen this
    context; on a session error, retry the other mode once (self-correcting —
    handles stale state / restarts where ``_seen_contexts`` was reset).
    """
    if runner is None:
        runner = _subprocess_runner

    repo_path = Path(cfg["repo_path"])
    session_uuid = session_id_for_context(context_id)
    mcp_config = resolve_mcp_config(repo_path)
    timeout = float(cfg.get("claude_timeout_s") or DEFAULTS["claude_timeout_s"])

    first_resume = _has_seen(context_id)
    attempts = [first_resume, not first_resume]
    last_reply: Optional[str] = None

    for resume in attempts:
        cmd = build_claude_command(
            prompt, session_uuid, cfg, resume=resume, mcp_config_path=mcp_config
        )
        try:
            stdout, rc = runner(cmd, str(repo_path), timeout)
        except subprocess.TimeoutExpired:
            return f"[error] claude turn timed out after {timeout}s"
        except Exception as exc:  # noqa: BLE001 - never crash the poll loop
            log.warning("claude invocation failed (%s)", exc)
            return f"[error] claude invocation failed: {exc}"

        reply = parse_claude_output(stdout)
        last_reply = reply
        if _is_session_error(reply, rc) and resume != attempts[-1]:
            log.info("session mode %s failed for ctx=%s; retrying other mode",
                     "resume" if resume else "first", context_id)
            continue
        _mark_seen(context_id)
        return reply

    _mark_seen(context_id)
    return last_reply


def _is_session_error(reply: Optional[str], rc: int) -> bool:
    """Heuristic: did the turn fail in a way a session-mode flip might fix?"""
    if rc == 0:
        return False
    if reply is None:
        return True
    low = reply.lower()
    return "session" in low and ("not found" in low or "exist" in low or "resume" in low)


def _subprocess_runner(cmd: List[str], cwd: str, timeout: float) -> Tuple[str, int]:
    """Real subprocess invocation of ``claude -p``. Returns (stdout, returncode)."""
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0 and proc.stderr:
        log.warning("claude stderr: %s", proc.stderr.strip()[:500])
    return proc.stdout, proc.returncode


# ---------------------------------------------------------------------------
# Transcript + reply
# ---------------------------------------------------------------------------

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
        with TRANSCRIPT_PATH.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as exc:
        log.warning("transcript write failed (%s)", exc)


def post_reply(hermes_url: str, context_id: str, text: str) -> bool:
    """POST the reply back to Hermes as JSON-RPC SendMessage. Returns success.

    Failure is logged and swallowed (never crashes the poll loop).
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
    try:
        req = urllib.request.Request(
            hermes_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            log.info("posted reply to hermes ctx=%s status=%s", context_id, resp.status)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to POST reply to hermes (%s)", exc)
        return False


# ---------------------------------------------------------------------------
# Inbox processing
# ---------------------------------------------------------------------------

class Receiver:
    """Owns the inbox poll loop + per-context serialization + reply dispatch."""

    def __init__(self, cfg: Dict[str, Any], runner: Any = None) -> None:
        self.cfg = cfg
        self.runner = runner
        self.locks = ContextLocks()
        self._processed = 0  # number of inbox lines already consumed
        self._stop = threading.Event()

    def process_message(self, context_id: str, text: str) -> Optional[str]:
        """Serialize per contextId, run the turn, POST the reply. Returns reply."""
        lock = self.locks.get(context_id)
        wait = float(self.cfg.get("context_lock_wait_s") or DEFAULTS["context_lock_wait_s"])
        acquired = lock.acquire(timeout=wait)
        if not acquired:
            busy = "[busy] this context is processing another turn; retry shortly"
            log.warning("ctx=%s busy; lock wait %.0fs exceeded", context_id, wait)
            _transcript("claude->hermes (busy)", "claude-code", "hermes", context_id, busy)
            post_reply(self.cfg["hermes_url"], context_id, busy)
            return busy
        try:
            reply = run_claude_turn(text, context_id, self.cfg, runner=self.runner)
            out = reply if reply is not None else "[no reply produced by claude]"
            _transcript("claude->hermes", "claude-code", "hermes", context_id, out)
            post_reply(self.cfg["hermes_url"], context_id, out)
            return reply
        finally:
            lock.release()

    def poll_once(self) -> None:
        """Drain new inbox lines, dispatching each on its own thread (per-context
        locking inside ``process_message`` serializes same-context turns)."""
        if not INBOX_PATH.exists():
            return
        try:
            lines = INBOX_PATH.read_text().splitlines()
        except OSError as exc:
            log.warning("inbox read failed (%s)", exc)
            return
        for idx in range(self._processed, len(lines)):
            line = lines[idx].strip()
            self._processed = idx + 1
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("from") != "hermes":
                continue
            context_id = entry.get("contextId") or "ctx-anon"
            text = entry.get("text", "")
            threading.Thread(
                target=self.process_message,
                args=(context_id, text),
                name=f"turn-{context_id[:12]}",
                daemon=True,
            ).start()

    def poll_loop(self) -> None:
        interval = float(self.cfg.get("poll_interval_s") or DEFAULTS["poll_interval_s"])
        log.info("inbox poll loop started (%.1fs)", interval)
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(interval)

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


def make_handler(cfg: Dict[str, Any], expected_token: Optional[str]) -> type:
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
            presented = header.split(None, 1)[1].strip()
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
            length = int(self.headers.get("Content-Length", 0) or 0)
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
            message = params.get("message") or {}
            context_id = message.get("contextId") or "ctx-anon"
            # Queue to inbox; the poll loop processes asynchronously.
            try:
                with INBOX_PATH.open("a") as f:
                    f.write(json.dumps({
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "from": "hermes",
                        "contextId": context_id,
                        "text": text,
                    }) + "\n")
            except OSError as exc:
                self._json(200, {"jsonrpc": "2.0", "id": rpc_id,
                                 "error": {"code": -32000, "message": f"inbox write failed: {exc}"}})
                return
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

def write_pid_file(path: Path = PID_PATH) -> None:
    try:
        path.write_text(str(os.getpid()))
    except OSError as exc:
        log.warning("could not write PID file %s (%s)", path, exc)


def remove_pid_file(path: Path = PID_PATH) -> None:
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

    INBOX_PATH.touch(exist_ok=True)
    TRANSCRIPT_PATH.touch(exist_ok=True)
    write_pid_file()

    expected_token = resolve_auth_token(cfg)
    if not expected_token:
        log.warning(
            "no bearer token configured (auth_token_env=%r) — POST /jsonrpc is OPEN. "
            "Acceptable only on a loopback dev bind.",
            cfg.get("auth_token_env"),
        )

    log_harness_inventory(repo_path)
    log.info("repo_path (cwd for claude) pinned to %s", repo_path)

    receiver = Receiver(cfg)
    poll_thread = threading.Thread(target=receiver.poll_loop, name="inbox-poll", daemon=True)
    poll_thread.start()

    handler = make_handler(cfg, expected_token)
    httpd = ThreadingHTTPServer((cfg["bind_host"], int(cfg["bind_port"])), handler)

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
