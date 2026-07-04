#!/usr/bin/env python3
"""Standalone A2A receiver — Google Antigravity CLI (``agy``) as an executor peer.

This file is a TEMPLATE. Hermes' ``deploy_agy_receiver`` tool copies it verbatim
into a target repo's ``<repo>/.hermes/agy_receiver.py`` and writes a sibling
``agy_receiver.json`` config. The receiver then runs as a detached daemon that:

  1. Serves an A2A surface on ``bind_host:bind_port``
     (GET /health, GET /.well-known/agent-card.json, POST /jsonrpc).
  2. Queues inbound messages to an inbox JSONL and ACKs immediately.
  3. A background poll loop drains the inbox, spawning ``agy --print`` with the
     repo as cwd (pinned from config, NEVER from inbound message).
  4. Maintains a persistent agy conversation per A2A ``contextId`` via a durable
     ``a2a-agy-sessions.json`` map + ``agy --conversation <uuid> --print``.
  5. POSTs the result back to ``hermes_url`` as a JSON-RPC SendMessage.

Design constraints (deliberate):
  * STDLIB ONLY (+ the ``agy`` CLI). No import of the a2a_fleet package, no
    Hermes gateway dependency — it must run on its own inside any repo.
  * ``cwd`` for agy is ALWAYS ``repo_path`` from config, NEVER taken from an
    inbound message (a remote peer must not be able to redirect execution).
  * Per-contextId serialization: two concurrent turns on the same contextId must
    NOT both mint / resume the same agy conversation concurrently. A per-contextId
    lock serializes same-context turns; different contextIds run concurrently.

agy CLI subprocess contract (agy v1.0.4, empirically probed):
  First turn (no stored conversation uuid):
    AGY_CLI_DISABLE_LATEX=1 agy --print "<prompt>" --dangerously-skip-permissions [--sandbox]
    * NO --model flag exists.
    * --sandbox is a BOOLEAN toggle (no value).
    * cwd = repo_path (pinned).
  Resume turn (stored conversation uuid exists for this contextId):
    AGY_CLI_DISABLE_LATEX=1 agy --conversation <uuid> --print "<prompt>" --dangerously-skip-permissions [--sandbox]
  * Do NOT use --continue (it is cwd-global last-session — unsafe for concurrent
    A2A contexts sharing a cwd). Always pin explicit --conversation <uuid> under
    the per-contextId lock.

SESSION ID DISCOVERY (the uuid is NOT caller-assignable):
  On turn 1 you cannot set the id. After a first turn agy generates a uuid and
  records it in ``~/.gemini/antigravity-cli/cache/last_conversations.json`` —
  a JSON object mapping cwd(repo_path) -> uuid. We READ that file after a first
  turn and persist contextId -> {conversation_id, last_stdout} on disk.

OUTPUT EXTRACTION (plain text on stdout; stderr empty on success):
  * First turn: stdout is just the reply; strip trailing newline.
  * RESUME turn: agy RE-ECHOES the ENTIRE prior transcript (all prior assistant
    replies, newline-separated, NO role/turn markers) then appends the new reply.

    Observed (agy v1.0.4) — probing /tmp/agy-probe:
      turn1 ("say: remembered the word BANANA") stdout:
        "remembered the word BANANA.\n"
      turn2 ("what word?") stdout:
        "remembered the word BANANA.\nBANANA\n"
      turn3 ("say only: THIRD") stdout:
        "remembered the word BANANA.\nBANANA\nTHIRD\n"

    There is NO delimiter — the cumulative stdout is just prior-replies +
    new-reply concatenated. So the extractor is PREFIX-STRIP: we persist the
    FULL prior stdout per contextId; on a resume turn the new full stdout begins
    with the prior full stdout as a literal prefix, so the latest reply is
    ``new_stdout[len(prior_stdout):]``. If the stored prefix does not match
    (drift / first resume after a restart with no stored prior), fall back to the
    LAST non-empty line of stdout. We persist the new full stdout after each turn
    for the next resume.

REMINT SIGNAL: if stdout contains ``Warning: conversation "<id>" not found.``
  (the stored uuid is dead), agy proceeds as a FRESH first turn (rc=0). We treat
  that as session-missing: clear the stored uuid + last_stdout BEFORE persisting,
  strip the Warning line from the reply, and re-read last_conversations.json to
  capture the NEW uuid agy minted.

AUTH: agy auth is macOS Keychain (no file, no headless login, no ``agy auth``).
  If a turn appears to fail because agy is not authenticated (it tends to hang to
  timeout, or emits an auth-related error), the receiver surfaces a clear error
  ("agy not authenticated — run `agy` interactively once to sign in") rather than
  hanging silently. The turn timeout itself is the hard backstop.

Reply contract (receiver -> Hermes), POSTed to ``hermes_url``::

    {"jsonrpc": "2.0", "id": "agy-<ts>", "method": "SendMessage",
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
CONFIG_PATH = SCRIPT_DIR / "agy_receiver.json"
INBOX_PATH = SCRIPT_DIR / "a2a-agy-inbox.jsonl"
INBOX_OFFSET_PATH = SCRIPT_DIR / "a2a-agy-inbox.offset"
TRANSCRIPT_PATH = SCRIPT_DIR / "a2a-agy-transcript.jsonl"
PID_PATH = SCRIPT_DIR / "agy_receiver.pid"
TOKEN_PATH = SCRIPT_DIR / ".agy-token"
SESSION_MAP_PATH = SCRIPT_DIR / "a2a-agy-sessions.json"

# agy records cwd -> conversation uuid here after a turn. We read it to discover
# the uuid agy minted for a first turn (the id is NOT caller-assignable).
AGY_LAST_CONVERSATIONS_PATH = (
    Path.home() / ".gemini" / "antigravity-cli" / "cache" / "last_conversations.json"
)

# Cap on a single inbound JSON-RPC body (DoS guard) and the prompt we hand to
# agy. 1 MiB body is generous for text tasks; oversized bodies are rejected
# with HTTP 413 before allocation.
MAX_BODY_BYTES = 1 * 1024 * 1024
MAX_PROMPT_CHARS = 256 * 1024
# Cap agy stdout we buffer in memory (defensive — runaway tool output).
MAX_STDOUT_BYTES = 8 * 1024 * 1024

# A signal in stdout that a stored conversation genuinely does not exist (the
# ONLY condition under which we remint a fresh first turn — see ``run_agy_turn``).
# agy emits this as the FIRST stdout line and then proceeds as a fresh turn.
SESSION_NOT_FOUND_PREFIX = 'warning: conversation "'
SESSION_NOT_FOUND_SUFFIX = '" not found.'

# Heuristic signals that a failed/empty turn was actually an auth problem (agy
# auth is macOS Keychain; a missing sign-in tends to hang or emit these).
AUTH_FAILURE_SIGNALS = (
    "not authenticated",
    "not signed in",
    "please sign in",
    "authentication required",
    "login required",
    "unauthorized",
    "no credentials",
)

AUTH_HELP = (
    "agy not authenticated — run `agy` interactively once on this host to sign in "
    "(macOS Keychain; there is no headless login)"
)

DEFAULTS: Dict[str, Any] = {
    "repo_path": str(SCRIPT_DIR.parent),  # .hermes/ is inside the repo
    "bind_host": "127.0.0.1",
    "bind_port": 9313,
    "hermes_url": "http://127.0.0.1:9219/jsonrpc",
    "role_prompt": (
        "You are a Google Antigravity CLI executor peer in an A2A fleet. The "
        "orchestrator is Hermes. You receive tasks over A2A and execute them in "
        "THIS repo using your full tools/skills. Reply concisely with "
        "results/status. Same contextId = same ongoing session/thread."
    ),
    "role_file": None,            # if set, read role prompt from this path (overrides role_prompt)
    "agy_sandbox": False,         # boolean: pass --sandbox when true
    "agy_extra_flags": [],        # list[str] appended verbatim to the command
    "auth_token_env": None,       # env var name holding the INBOUND bearer token (POST /jsonrpc)
    "hermes_auth_token_env": None,  # env var name holding the bearer token for OUTBOUND replies to Hermes
    "poll_interval_s": 2.0,
    # Real repo work (multi-file edits, gh calls) routinely exceeds agy's own 5m
    # --print-timeout default, which is what produced plan-only/no-result turns
    # (#100). 15m is a sane budget for autonomous tasks; raise per deploy if needed.
    "agy_timeout_s": 900,
    "context_lock_wait_s": 600.0,  # how long a queued same-context turn waits for the lock
    "max_concurrent_turns": 3,     # global cap on simultaneous agy subprocesses
    "max_tracked_contexts": 1024,  # bound on the per-context lock registry
    "idle_timeout_s": 1800,        # self-teardown after this many idle seconds (0 = disabled)
}

# Receiver subprocess backstop = agy_print_timeout + this grace, so agy reaches
# its OWN --print-timeout first and exits cleanly instead of being killpg'd.
AGY_TIMEOUT_GRACE_S = 60.0


def _print_timeout_s(cfg: Dict[str, Any]) -> int:
    """agy --print-timeout in whole seconds, ceil'd, min 1.

    Single source for BOTH the flag value (build_agy_command) and the receiver
    backstop (run_agy_turn) so the invariant `backstop > print_timeout` holds for
    any configured value — a fractional/tiny agy_timeout_s must not truncate to
    `0s` (agy would self-timeout immediately / reject the arg).
    """
    raw = float(cfg.get("agy_timeout_s") or DEFAULTS["agy_timeout_s"])
    whole = int(raw)
    if raw > whole:
        whole += 1  # ceil
    return max(1, whole)

# Common tool dirs appended to PATH for the spawned agy process. A receiver
# launched by launchd (or any non-login daemon) inherits a minimal PATH, so
# agy's terminal/tool calls can't find `gh`/`git`/node. We APPEND (never shadow)
# so an explicit parent PATH still wins; only missing dirs are added.
_EXTRA_PATH_DIRS = (
    "/opt/homebrew/bin", "/opt/homebrew/sbin",
    "/usr/local/bin", "/usr/local/sbin",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
)


def _tool_env() -> Dict[str, str]:
    """os.environ copy with AGY_CLI_DISABLE_LATEX=1 + common tool dirs on PATH."""
    env = dict(os.environ)
    env["AGY_CLI_DISABLE_LATEX"] = "1"
    parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    for d in (os.path.expanduser("~/.local/bin"), *_EXTRA_PATH_DIRS):
        if d and d not in parts and os.path.isdir(d):
            parts.append(d)
    env["PATH"] = os.pathsep.join(parts)
    return env

log = logging.getLogger("agy_receiver")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(config_path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """Read ``agy_receiver.json`` (sibling of this script), merge over DEFAULTS.

    Missing / malformed config is non-fatal: defaults are used and a warning is
    logged. ``role_file`` (if set + readable) supplies the role prompt.
    """
    cfg = dict(DEFAULTS)
    cfg["agy_extra_flags"] = list(DEFAULTS["agy_extra_flags"])
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

    if not isinstance(cfg.get("agy_extra_flags"), list):
        log.warning("agy_extra_flags is not a list; ignoring")
        cfg["agy_extra_flags"] = []

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
# Durable agy conversation map (contextId -> {conversation_id, last_stdout})
# ---------------------------------------------------------------------------

_SESSION_MAP_LOCK = threading.Lock()

# Process-global FIRST-TURN lock. agy records its minted conversation uuid in
# last_conversations.json keyed ONLY by repo cwd. This receiver is
# one-process-per-repo, so EVERY contextId shares that single cwd key. Two
# DIFFERENT contextIds doing their FIRST turn concurrently would both read the
# same cwd key and cross-capture each other's uuid. We serialize ONLY the
# first-turn critical section [spawn first turn -> read last_conversations.json
# -> persist contextId->uuid]; RESUME turns (stored uuid already known) skip
# this lock entirely so the common path keeps full concurrency. Because one
# receiver == one cwd, a single global lock is sufficient — no per-cwd dict
# needed. Ordering: process_message acquires the per-context lock FIRST, then
# run_agy_turn acquires this lock (first turns only); nothing acquires them in
# the opposite order, so no deadlock.
_FIRST_TURN_LOCK = threading.Lock()


def load_session_map(path: Path = SESSION_MAP_PATH) -> Dict[str, Dict[str, Any]]:
    """Load the durable agy conversation map. Malformed content -> empty map.

    Each entry: {"conversation_id": <uuid>, "last_stdout": <str>, "updated_at": <int>}.
    ``last_stdout`` is the FULL cumulative stdout of the most recent turn — used
    as the literal prefix to strip on the next resume turn (see module docstring).
    """
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
        conversation_id = entry.get("conversation_id")
        if isinstance(conversation_id, str) and conversation_id.strip():
            last_stdout = entry.get("last_stdout")
            updated_at = entry.get("updated_at")
            rec: Dict[str, Any] = {
                "conversation_id": conversation_id.strip(),
                "last_stdout": last_stdout if isinstance(last_stdout, str) else "",
                "updated_at": int(updated_at) if isinstance(updated_at, (int, float)) else int(time.time()),
            }
            # Preserve the drift flag (#108/#109) across reads — it is what makes
            # the silent extractor fallback observable to the dashboard / Hermes.
            if entry.get("prefix_drifted") is True:
                rec["prefix_drifted"] = True
                drifted_at = entry.get("drifted_at")
                if isinstance(drifted_at, (int, float)):
                    rec["drifted_at"] = int(drifted_at)
            else:
                rec["prefix_drifted"] = False
            clean[context_id] = rec
    return clean


def get_session_entry(context_id: str, path: Path = SESSION_MAP_PATH) -> Optional[Dict[str, Any]]:
    """Return the stored session entry for ``context_id`` (or None)."""
    return load_session_map(path).get(context_id)


def get_conversation_id_for_context(context_id: str, path: Path = SESSION_MAP_PATH) -> Optional[str]:
    """Return the stored agy conversation uuid for ``context_id`` (or None)."""
    entry = get_session_entry(context_id, path) or {}
    conversation_id = entry.get("conversation_id")
    if isinstance(conversation_id, str) and conversation_id.strip():
        return conversation_id.strip()
    return None


def _write_session_map(data: Dict[str, Dict[str, Any]], path: Path) -> None:
    """Atomic tmp+os.replace write of the session map. Caller holds the lock."""
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


def store_session_for_context(
    context_id: str,
    conversation_id: str,
    last_stdout: str,
    path: Path = SESSION_MAP_PATH,
    *,
    prefix_drifted: bool = False,
) -> None:
    """Persist contextId -> {conversation_id, last_stdout, prefix_drifted} atomically.

    ``prefix_drifted`` records whether THIS turn's resume output failed to match
    the persisted ``last_stdout`` prefix (issue #108) — a restart/crash left the
    receiver's baseline out of sync with agy's server-side conversation. The flag
    (+ ``drifted_at`` when set) turns a previously-silent extractor fallback into
    a machine-observable event the dashboard / Hermes can surface (#109).
    """
    if not conversation_id.strip():
        return
    now = int(time.time())
    with _SESSION_MAP_LOCK:
        data = load_session_map(path)
        record = {
            "conversation_id": conversation_id.strip(),
            "last_stdout": last_stdout if isinstance(last_stdout, str) else "",
            "prefix_drifted": bool(prefix_drifted),
            "updated_at": now,
        }
        if prefix_drifted:
            record["drifted_at"] = now
        data[context_id] = record
        _write_session_map(data, path)


def clear_session_for_context(context_id: str, path: Path = SESSION_MAP_PATH) -> None:
    """Remove a stale/dead conversation entry for ``context_id`` from the map.

    Called under the per-contextId lock BEFORE a remint retry so that a failed
    remint leaves the map clean (no stale uuid re-persisted on the next turn).
    """
    with _SESSION_MAP_LOCK:
        data = load_session_map(path)
        if context_id not in data:
            return
        del data[context_id]
        _write_session_map(data, path)


# ---------------------------------------------------------------------------
# agy conversation-id discovery (read last_conversations.json)
# ---------------------------------------------------------------------------

def discover_conversation_id(
    repo_path: Path,
    last_conversations_path: Path = AGY_LAST_CONVERSATIONS_PATH,
) -> Optional[str]:
    """Read the conversation uuid agy minted for ``repo_path`` (its cwd).

    agy records ``cwd -> conversation_uuid`` in last_conversations.json after a
    turn. We look it up by the canonical repo path (agy keys by the cwd it ran
    in). Missing / malformed file -> None.
    """
    try:
        raw = json.loads(last_conversations_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    # agy keys by the literal cwd string. Try both the canonical path and the
    # realpath form (macOS /tmp -> /private/tmp) to be robust.
    candidates = [str(repo_path), os.path.realpath(str(repo_path))]
    for key in candidates:
        cid = raw.get(key)
        if isinstance(cid, str) and cid.strip():
            return cid.strip()
    return None


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


def build_agy_command(
    prompt: str,
    cfg: Dict[str, Any],
    *,
    conversation_id: Optional[str] = None,
) -> List[str]:
    """Build the ``agy --print`` argv for one turn.

    First turn (no conversation_id):
        agy --print "<prompt>" --dangerously-skip-permissions --add-dir <repo> --print-timeout <N>s [--sandbox]
    Resume turn (conversation_id stored):
        agy --conversation <uuid> --print "<prompt>" --dangerously-skip-permissions --add-dir <repo> --print-timeout <N>s [--sandbox]

    IMPORTANT:
      * NO --model flag exists in agy.
      * --add-dir <repo_path> grants agy read/write access to the pinned repo so
        tool calls (file ops, terminal) operate on it — WITHOUT it agy treats the
        task as out-of-workspace and returns only a plan (issue #100).
      * --print-timeout caps agy's own non-interactive wait. agy's DEFAULT is 5m;
        real repo tasks routinely exceed that and exit plan-only. We pin it to the
        receiver's configured turn budget (cfg["agy_timeout_s"]); the receiver's
        own subprocess backstop is set strictly LONGER (grace) so agy self-exits
        first with a clean result rather than being killpg'd mid-write.
      * --sandbox is a BOOLEAN toggle (added when cfg["agy_sandbox"] is truthy).
        Do NOT rely on --sandbox + --dangerously-skip-permissions together —
        skip-permissions also auto-approves sandbox-escape (upstream agy bug);
        they are independent knobs here and sandbox defaults off.
      * --conversation pins an explicit uuid (never --continue, which is
        cwd-global and unsafe for concurrent contexts sharing a cwd).
      * AGY_CLI_DISABLE_LATEX=1 is set in the subprocess ENV, not as a flag.
    """
    full_prompt = _prompt_with_role(prompt, cfg)
    repo_path = str(cfg.get("repo_path") or "")
    print_timeout_s = _print_timeout_s(cfg)
    cmd: List[str] = ["agy"]
    if conversation_id:
        cmd += ["--conversation", conversation_id]
    cmd += ["--print", full_prompt, "--dangerously-skip-permissions"]
    if repo_path:
        cmd += ["--add-dir", repo_path]
    cmd += ["--print-timeout", f"{print_timeout_s}s"]
    if cfg.get("agy_sandbox"):
        cmd += ["--sandbox"]
    extra = cfg.get("agy_extra_flags") or []
    if isinstance(extra, list):
        cmd += _sanitize_extra_flags(extra)
    return cmd


# Flags that would break the --conversation resume model or collide with the
# flags we always set. Stripped from agy_extra_flags so a stale config cannot
# inject a conflicting session selector.
_FORBIDDEN_EXTRA: frozenset = frozenset(
    {
        "--continue", "-c", "--conversation", "--print", "-p", "--prompt",
        "--prompt-interactive", "-i",
        # Managed by build_agy_command — must not be overridden via extra_flags.
        "--add-dir", "--print-timeout",
    }
)


def _sanitize_extra_flags(extra: List[str]) -> List[str]:
    """Return a copy of ``extra`` with forbidden session/print flags removed.

    Handles both ``--flag value`` (two tokens) and ``--flag=value`` (one token).
    Forbidden flags would conflict with the explicit --conversation/--print we
    always set; logs a warning for each dropped token.
    """
    result: List[str] = []
    tokens = [str(x) for x in extra]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        base = tok.split("=", 1)[0] if "=" in tok else tok
        if base in _FORBIDDEN_EXTRA:
            log.warning("dropping forbidden agy_extra_flags token %r", tok)
            i += 1
            if "=" not in tok and i < len(tokens) and not tokens[i].startswith("-"):
                log.warning("dropping forbidden agy_extra_flags value token %r", tokens[i])
                i += 1
            continue
        result.append(tok)
        i += 1
    return result


# ---------------------------------------------------------------------------
# Deterministic result parsing (plain text, prefix-strip extraction)
# ---------------------------------------------------------------------------

def is_session_not_found(stdout: str) -> bool:
    """True if stdout's first line is the agy ``conversation "<id>" not found`` warning."""
    if not stdout or not stdout.strip():
        return False
    first = stdout.strip().splitlines()[0].strip().lower()
    return first.startswith(SESSION_NOT_FOUND_PREFIX) and first.endswith(SESSION_NOT_FOUND_SUFFIX)


def strip_not_found_warning(stdout: str) -> str:
    """Remove a leading ``Warning: conversation "<id>" not found.`` line, if present.

    agy prints this warning as the first stdout line then proceeds as a fresh
    first turn, so the remaining lines are the real reply.
    """
    if not stdout:
        return stdout
    lines = stdout.splitlines()
    # Skip any leading blank lines + the warning line.
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines):
        low = lines[idx].strip().lower()
        if low.startswith(SESSION_NOT_FOUND_PREFIX) and low.endswith(SESSION_NOT_FOUND_SUFFIX):
            idx += 1
    return "\n".join(lines[idx:]).strip()


def looks_like_auth_failure(stdout: str, stderr: str) -> bool:
    """True if stdout/stderr contains a recognizable agy auth-failure signal."""
    for hay in (stdout or "", stderr or ""):
        low = hay.lower()
        for sig in AUTH_FAILURE_SIGNALS:
            if sig in low:
                return True
    return False


def extract_reply(stdout: str, prior_stdout: Optional[str]) -> Optional[str]:
    """Extract only the latest reply block from a turn's stdout.

    First turn (prior_stdout falsy): the whole stdout is the reply -> stripped.

    Resume turn: agy re-echoes the entire prior transcript as a literal prefix,
    then appends the new reply. So we strip ``prior_stdout`` from the front of
    ``stdout``. Fallbacks (in order) when the prefix does not match exactly:
      1. prior_stdout (rstrip) is a prefix -> strip that.
      2. No prefix match -> return the FULL stdout (stripped). We deliberately
         do NOT fall back to the last line: over-returning the re-echoed
         transcript is visible and recoverable, whereas dropping earlier lines
         of a genuine multi-line reply silently loses content.

    Returns the stripped reply, or None when stdout has no usable text.
    """
    if stdout is None:
        return None
    text = stdout
    if prior_stdout:
        # agy may differ only by trailing newline; try exact then rstripped prefix.
        if text.startswith(prior_stdout):
            tail = text[len(prior_stdout):]
            return tail.strip() or None
        rprior = prior_stdout.rstrip("\n")
        if rprior and text.startswith(rprior):
            tail = text[len(rprior):]
            return tail.strip() or None
        # Prefix drifted (e.g. restart lost prior_stdout): return the WHOLE
        # stdout rather than just the tail line, so multi-line replies survive.
        return text.strip() or None
    return text.strip() or None


# Returned (instead of the opaque "[no reply produced by agy]") when a drifted
# resume turn yields no extractable reply — tells the reader the receiver lost
# its prefix baseline, not that agy failed to answer (#108/#109).
DRIFT_REPLY_MSG = (
    "[drift detected — persisted last_stdout does not match agy's cumulative "
    "output; receiver baseline lost after a restart]"
)


def _prefix_drifted(stdout: str, prior_stdout: Optional[str]) -> bool:
    """True when a RESUME turn's stdout does not start with the persisted prior
    transcript — the receiver's baseline drifted from agy's server-side
    conversation (e.g. after a restart, #108). Mirrors extract_reply's prefix
    match (exact, then rstripped). First turns (no prior) never drift.
    """
    if not prior_stdout or stdout is None:
        return False
    if stdout.startswith(prior_stdout):
        return False
    rprior = prior_stdout.rstrip("\n")
    if rprior and stdout.startswith(rprior):
        return False
    return True


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
# (legacy 2-tuple runners are still accepted for backward compatibility.)


class AgyCLINotFound(Exception):
    """Raised by the runner when the ``agy`` binary is not on PATH."""


def run_agy_turn(
    prompt: str,
    context_id: str,
    cfg: Dict[str, Any],
    *,
    runner: Any = None,
) -> Optional[str]:
    """Run one agy turn for ``context_id`` with prefix-strip extraction + remint.

    Flow:
      * Look up the stored conversation uuid + last_stdout for this context.
      * Build the command (resume when a uuid exists, else first turn).
      * After the turn: if the stored uuid was missing (agy printed the
        ``not found`` warning), REMINT — clear the stored entry, treat the same
        invocation (agy already ran fresh) as a first turn, and capture the new
        uuid agy minted (re-read last_conversations.json).
      * Capture the conversation uuid (from last_conversations.json, keyed by the
        pinned repo cwd) and persist {conversation_id, full stdout} for the next
        resume's prefix-strip.
    """
    if runner is None:
        runner = _subprocess_runner

    session_map_path: Path = SESSION_MAP_PATH  # may be monkeypatched in tests
    repo_path = Path(cfg["repo_path"])
    # agy gets --print-timeout == _print_timeout_s(cfg) (see build_agy_command);
    # the receiver's own subprocess backstop must be strictly longer so agy hits
    # its OWN timeout and self-exits with a clean result instead of being killpg'd
    # mid-turn. Same source on both sides keeps the invariant for any config value.
    timeout = _print_timeout_s(cfg)
    backstop = timeout + AGY_TIMEOUT_GRACE_S

    entry = get_session_entry(context_id, session_map_path) or {}
    stored_cid = entry.get("conversation_id") if isinstance(entry.get("conversation_id"), str) else None
    prior_stdout = entry.get("last_stdout") if isinstance(entry.get("last_stdout"), str) else None

    def _invoke(conversation_id: Optional[str]) -> Tuple[str, int, str]:
        cmd = build_agy_command(prompt, cfg, conversation_id=conversation_id)
        return _call_runner(runner, cmd, str(repo_path), backstop)

    # A turn that has no stored uuid MUST mint one (agy keys it by cwd, which is
    # shared by all contextIds in this one-process-per-repo receiver). Serialize
    # the mint critical section [spawn -> discover -> persist] under the global
    # first-turn lock so two different contextIds can't cross-capture each
    # other's uuid. Resume turns (stored_cid known) skip the lock for full
    # concurrency. The per-context lock is already held by process_message; we
    # acquire the first-turn lock strictly inside it, never the reverse.
    is_first_turn = stored_cid is None
    if is_first_turn:
        _FIRST_TURN_LOCK.acquire()
    try:
        try:
            stdout, rc, stderr = _invoke(stored_cid)
        except subprocess.TimeoutExpired:
            # A hang to timeout is the canonical agy auth-failure symptom.
            return f"[error] agy turn timed out after {timeout}s ({AUTH_HELP})"
        except (FileNotFoundError, AgyCLINotFound):
            return "[error] agy CLI not found on PATH"
        except Exception as exc:  # noqa: BLE001
            log.warning("agy invocation failed (%s)", exc)
            return f"[error] agy invocation failed: {exc}"

        # REMINT: a resume against a dead uuid prints the not-found warning then
        # runs fresh in the SAME invocation. It mints a NEW uuid keyed by cwd, so
        # its discover+persist needs the same first-turn serialization. Acquire
        # the lock now (we did not hold it, since this began as a resume). Clear
        # the stale entry so a failed discovery does not re-persist the dead uuid.
        # We do NOT re-run (agy already executed the prompt fresh — a second run
        # would double-execute a side-effecting turn). When stored_cid is None at
        # entry this never fires (already under the lock).
        reminted = False
        if stored_cid and is_session_not_found(stdout):
            log.info("stored conversation %s missing for ctx=%s; reminting", stored_cid, context_id)
            _FIRST_TURN_LOCK.acquire()
            is_first_turn = True  # ensure release in finally
            clear_session_for_context(context_id, session_map_path)
            stored_cid = None
            prior_stdout = None
            reminted = True

        # Empty output = failed turn. agy v1.0.4 `--print` exits rc=0 with EMPTY
        # stdout/stderr when not signed in — a SILENT failure with no marker
        # string and no hang, so the old `empty AND auth-marker` heuristic missed
        # it and the turn fell through to the opaque "[no reply produced by agy]"
        # fallback (#105). Treat ANY empty turn as the actionable sign-in failure;
        # a non-empty turn carrying an explicit auth marker is surfaced the same.
        if (not stdout.strip()) or looks_like_auth_failure(stdout, stderr):
            return f"[error] {AUTH_HELP}"

        # Reply extraction. On a remint the warning line is stripped and there is
        # no valid prior prefix, so use first-turn semantics on the stripped text.
        if reminted:
            cleaned = strip_not_found_warning(stdout)
            reply: Optional[str] = cleaned.strip() or None
            full_stdout_for_persist = cleaned
        else:
            reply = extract_reply(stdout, prior_stdout)
            full_stdout_for_persist = stdout

        # Prefix drift (#108): a resume turn whose stdout is not the persisted
        # prior transcript means the receiver's baseline is out of sync with
        # agy's server-side conversation (typically after a restart). Record it
        # as a first-class flag (#109) so the silent extractor fallback becomes
        # observable, and give an honest reply if nothing extractable came out.
        drifted = (not reminted) and _prefix_drifted(stdout, prior_stdout)
        if drifted:
            log.warning(
                "prefix drift for ctx=%s: persisted last_stdout is not a prefix "
                "of agy's resume output (receiver likely restarted mid-session, #108)",
                context_id,
            )
            if reply is None:
                reply = DRIFT_REPLY_MSG

        # Capture + persist the conversation uuid for the next resume. On a fresh
        # first turn (or a remint) agy minted a NEW uuid recorded in
        # last_conversations.json; on a resume the uuid is unchanged (reuse
        # stored).
        new_cid = stored_cid or discover_conversation_id(repo_path)
        if new_cid:
            store_session_for_context(
                context_id, new_cid, full_stdout_for_persist, session_map_path,
                prefix_drifted=drifted,
            )
        else:
            log.warning(
                "could not discover agy conversation uuid for ctx=%s (cwd=%s); "
                "continuity disabled for this turn", context_id, repo_path,
            )
    finally:
        if is_first_turn:
            _FIRST_TURN_LOCK.release()

    if reply is None and rc != 0:
        snippet = (stderr or "").strip().replace("\n", " ")[:300]
        if looks_like_auth_failure(stdout, stderr):
            return f"[error] {AUTH_HELP}"
        reply = (
            f"[error] agy exited rc={rc} with no parseable output"
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


def _subprocess_runner(cmd: List[str], cwd: str, timeout: float) -> Tuple[str, int, str]:
    """Real subprocess invocation of ``agy --print``.

    Uses ``Popen`` + ``start_new_session=True`` so the whole process tree lands in
    its own process group; on timeout we ``killpg`` the group to reap orphans.
    AGY_CLI_DISABLE_LATEX=1 + an augmented PATH (gh/git/node under a launchd
    daemon) are injected into the child env via _tool_env(). Returns (stdout, rc,
    stderr). Buffers are capped defensively.
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
        raise AgyCLINotFound("agy") from None
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
        log.warning("agy stderr: %s", stderr.strip()[:500])
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
        "id": f"agy-{int(time.time() * 1000)}",
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
        self._hermes_auth_token = resolve_hermes_auth_token(cfg)
        max_ctx = int(cfg.get("max_tracked_contexts") or DEFAULTS["max_tracked_contexts"])
        self.locks = ContextLocks(max_entries=max_ctx)
        self.inbox_path = inbox_path
        self.offset_path = offset_path
        self._processed = _read_offset(offset_path)
        self._offset_lock = threading.Lock()
        self._stop = threading.Event()
        max_turns = int(cfg.get("max_concurrent_turns") or DEFAULTS["max_concurrent_turns"])
        self._turn_slots = threading.BoundedSemaphore(max(1, max_turns))
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
            _transcript("agy->hermes (busy)", "agy", "hermes", context_id, busy)
            post_reply(self.cfg["hermes_url"], context_id, busy, self._hermes_auth_token)
            return busy
        try:
            lock = self.locks.get(context_id)
            acquired = lock.acquire(timeout=wait)
            if not acquired:
                busy = "[busy] this context is processing another turn; retry shortly"
                log.warning("ctx=%s busy; lock wait %.0fs exceeded", context_id, wait)
                _transcript("agy->hermes (busy)", "agy", "hermes", context_id, busy)
                post_reply(self.cfg["hermes_url"], context_id, busy, self._hermes_auth_token)
                return busy
            try:
                reply = run_agy_turn(
                    text, context_id, self.cfg, runner=self.runner
                )
                out = reply if reply is not None else "[no reply produced by agy]"
                _transcript("agy->hermes", "agy", "hermes", context_id, out)
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
        "name": "agy",
        "description": "Google Antigravity CLI repo-scoped A2A executor peer (agy_receiver).",
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
                "description": "Executes A2A tasks in the bound repo via agy --print with full harness.",
                "tags": ["v0.1", "agy", "antigravity", "executor"],
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
                    "name": "agy",
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
            if "contextId" in params:
                self._json(200, {"jsonrpc": "2.0", "id": rpc_id,
                                 "error": {"code": -32602,
                                           "message": "contextId must be nested under params.message, "
                                                      "not at params root (A2A spec)"}})
                return
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
            _transcript("hermes->agy", "hermes", "agy", context_id, text)
            ack = "Message received; executing in repo via Antigravity CLI. Reply will follow. [queued]"
            _transcript("agy->hermes (ack)", "agy", "hermes", context_id, ack)
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
        ".gemini": (Path.home() / ".gemini").exists(),
    }
    log.info(
        "harness inventory for %s: AGENTS.md=%s .mcp.json=%s ~/.gemini=%s",
        repo_path, inventory["AGENTS.md"], inventory[".mcp.json"], inventory[".gemini"],
    )
    return inventory


# ---------------------------------------------------------------------------
# PID file
# ---------------------------------------------------------------------------

def is_loopback_bind(host: str) -> bool:
    """True if ``host`` is a loopback address (auth may be optional there)."""
    return str(host).strip().lower() in {"127.0.0.1", "::1", "localhost", ""}


def probe_agy_cli() -> bool:
    """Best-effort ``agy --help`` probe; loud warning if missing. Non-fatal.

    agy has no ``--version`` flag; ``--help`` exits 0 and is cheap. Auth status
    cannot be probed without an interactive sign-in, so we only check presence.
    """
    try:
        env = _tool_env()
        proc = subprocess.run(
            ["agy", "--help"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        if proc.returncode == 0:
            log.info("agy CLI present (--help ok)")
            return True
        log.warning("agy --help exited rc=%s: %s", proc.returncode,
                    (proc.stderr or "").strip()[:200])
        return False
    except FileNotFoundError:
        log.warning("agy CLI NOT FOUND on PATH — turns will fail fatally "
                    "with '[error] agy CLI not found on PATH'")
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("agy --help probe failed (%s)", exc)
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
        format="%(asctime)s [agy_receiver] %(levelname)s %(message)s",
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

    probe_agy_cli()
    log_harness_inventory(repo_path)
    log.info("repo_path (cwd for agy) pinned to %s", repo_path)

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

    log.info("agy_receiver listening on http://%s:%s", cfg["bind_host"], cfg["bind_port"])
    try:
        httpd.serve_forever()
    finally:
        receiver.stop()
        remove_pid_file()
        log.info("agy_receiver stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
