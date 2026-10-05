"""
Shared helpers for dag_executor.py and node executors.

Ports executor-shared.ts: error classification, subprocess failure formatting,
variable substitution, node output ref substitution.
"""

from __future__ import annotations

import asyncio
import atexit
import codecs
import json
import logging
import os
import re
import signal
from typing import Any, Callable, Dict, Optional

from engine.schemas.workflow_run import NodeOutput

logger = logging.getLogger("workflow.executor-shared")


# ── Subprocess helpers (bash / script / loop until_bash) ────────────────────

_WF_VAR_ENV = {
    "workflow_id": "WORKFLOW_ID",
    "user_message": "USER_MESSAGE",
    "artifacts_dir": "ARTIFACTS_DIR",
    "base_branch": "BASE_BRANCH",
    "docs_dir": "DOCS_DIR",
}


NODE_OUTPUT_REF_RE = re.compile(
    r"\$([a-zA-Z_][a-zA-Z0-9_-]*)\.output(?:\.([a-zA-Z_][a-zA-Z0-9_]*))?"
)


def node_output_env_name(node_id: str) -> str:
    """Env var carrying a node's output to script nodes: NODE_<ID>_OUTPUT."""
    return "NODE_" + re.sub(r"\W", "_", node_id).upper() + "_OUTPUT"


_RESERVED_ENV = {"PATH", "PYTHONPATH", "HOME", "SHELL", "IFS", "BASH_ENV", "ENV"}
_RESERVED_PREFIXES = ("LD_", "DYLD_", "HERMES_", "PYTHON", "NODE_")


def reserved_input_name(name: str) -> bool:
    """Input names that would hijack the subprocess env (PATH, LD_PRELOAD, …)."""
    up = name.upper()
    return up in _RESERVED_ENV or up.startswith(_RESERVED_PREFIXES)


def workflow_env(ctx: Any, node_outputs: Optional[Dict[str, NodeOutput]] = None) -> Dict[str, str]:
    """Subprocess env: host env + workflow inputs + workflow vars (+ node outputs).

    Inputs are exported under their own names so bodies read ``"$repo_path"``
    (bash) or ``os.environ["repo_path"]`` (script) — never source-interpolated.
    """
    env = dict(os.environ)
    wf_vars = getattr(ctx, "workflow_vars", None) or {}
    for key, value in (wf_vars.get("inputs") or {}).items():
        if isinstance(key, str) and key.isidentifier() and value is not None \
                and not reserved_input_name(key):
            env[key] = value if isinstance(value, str) else json.dumps(value)
    for key, name in _WF_VAR_ENV.items():
        if wf_vars.get(key):
            env[name] = str(wf_vars[key])
    for node_id, out in (node_outputs or {}).items():
        if getattr(out, "state", None) == "completed":
            env[node_output_env_name(node_id)] = out.output or ""
    return env


def _num(obj: Any, name: str) -> Optional[float]:
    v = getattr(obj, name, None)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def usage_payload(result: Any) -> Optional[Dict[str, Any]]:
    """Token usage of a ``ctx.llm.complete()`` result as a node event payload.

    ``cost_usd`` is the provider's figure, else estimated from the host
    pricing table, else None (unknown pricing). None when no tokens reported.
    """
    u = getattr(result, "usage", None)
    inp, out = int(_num(u, "input_tokens") or 0), int(_num(u, "output_tokens") or 0)
    total = int(_num(u, "total_tokens") or inp + out)
    if not total:
        return None
    model = getattr(result, "model", None)
    provider = getattr(result, "provider", None)
    model = model if isinstance(model, str) and model else None
    provider = provider if isinstance(provider, str) and provider else None
    cost = _num(u, "cost_usd")
    if cost is None and model:
        try:
            from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
            cr, cw = int(_num(u, "cache_read_tokens") or 0), int(_num(u, "cache_write_tokens") or 0)
            # ponytail: assumes OpenAI-shaped input_tokens (cache included);
            # PluginLlmUsage doesn't say which shape it got.
            amount = estimate_usage_cost(model, CanonicalUsage(
                input_tokens=max(0, inp - cr - cw), output_tokens=out,
                cache_read_tokens=cr, cache_write_tokens=cw,
            ), provider=provider).amount_usd
            cost = float(amount) if amount is not None else None
        except Exception as exc:  # pricing is best-effort, never fails a node
            logger.debug("usage_payload.cost_estimate_failed model=%s error=%s", model, exc)
    return {
        "input_tokens": inp, "output_tokens": out, "total_tokens": total,
        "cost_usd": cost, "model": model, "provider": provider,
    }


def complete_with_usage(llm: Any, messages: Any, **kw: Any) -> Any:
    """``llm.complete`` + ``usage_payload`` in one blocking call, for
    run_in_executor: pricing may hit the network, so keep it off the loop.
    Returns ``(result, usage)``."""
    result = llm.complete(messages, **kw)
    return result, usage_payload(result)


def add_usage(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Sum two usage payloads (loop iterations). Cost is None if either is unknown."""
    if not a or not b:
        return a or b
    return {
        **{k: a[k] + b[k] for k in ("input_tokens", "output_tokens", "total_tokens")},
        "cost_usd": a["cost_usd"] + b["cost_usd"]
        if a["cost_usd"] is not None and b["cost_usd"] is not None else None,
        "model": b["model"] or a["model"],
        "provider": b["provider"] or a["provider"],
    }


def substitute_inputs(text: str, inputs: Dict[str, Any]) -> str:
    """Replace ``$<input_name>`` in LLM prompt text (never in code bodies —
    bash/script read inputs from env)."""
    if not inputs:
        return text

    def repl(m: re.Match) -> str:
        if m.group(1) not in inputs:
            return m.group(0)
        v = inputs[m.group(1)]
        return v if isinstance(v, str) else json.dumps(v)

    # One pass, and never touch `$x.output` refs, so neither an input name
    # nor a substituted value can rewrite node-output references.
    return re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)\b(?![.-]?[A-Za-z0-9_-]*\.output)", repl, text)


def subprocess_cwd(ctx: Any) -> Optional[str]:
    cwd = getattr(ctx, "cwd", None)
    return cwd if cwd and os.path.isdir(cwd) else None


# Process groups of node subprocesses still running. start_new_session puts
# them outside the host's group, so a gateway restart would orphan them;
# kill whatever is left at interpreter exit.
_LIVE_PGIDS: set = set()


@atexit.register
def _kill_live_groups() -> None:
    killpg = getattr(os, "killpg", None)
    for pgid in list(_LIVE_PGIDS):
        try:
            if killpg is not None:
                killpg(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass


async def communicate_or_kill(proc: "asyncio.subprocess.Process", timeout: float):
    """communicate() with timeout; on timeout/cancel kill the whole process group.

    Spawn with ``start_new_session=True`` so the group is the child's own.
    Re-raises TimeoutError / CancelledError after the child is reaped.
    """
    _LIVE_PGIDS.add(proc.pid)
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except BaseException:
        killpg = getattr(os, "killpg", None)
        try:
            if killpg is not None:
                killpg(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
        await proc.wait()
        raise
    finally:
        _LIVE_PGIDS.discard(proc.pid)


NODE_LOG_MAX_BYTES = 256 * 1024  # live log emitted per node, then one truncated marker
NODE_LOG_FLUSH_S = 0.25
NODE_LOG_FLUSH_BYTES = 8 * 1024


def node_log_emitter(ctx: Any, node_id: str) -> Callable[[Dict[str, Any]], None]:
    """on_chunk for ``stream_subprocess``: each chunk becomes a node_log event."""
    return lambda part: ctx.emit_event("node_log", {"run_id": ctx.run_id, "node_id": node_id, **part})


async def stream_subprocess(
    proc: "asyncio.subprocess.Process",
    timeout: float,
    on_chunk: Optional[Callable[[Dict[str, Any]], None]],
):
    """``communicate_or_kill`` + live output: same return / kill / raise contract.

    Both pipes are drained concurrently (a full stderr pipe can't block a
    stdout reader). Output is batched per stream and handed to ``on_chunk``
    as ``{stream, text, seq_in_node}`` every 250ms or 8KB; after
    NODE_LOG_MAX_BYTES one ``{stream, text: "", truncated: True}`` follows and
    the rest is only returned, not streamed. ``WORKFLOW_NODE_LOG=0`` (read per
    call) or no ``on_chunk`` falls back to plain ``communicate_or_kill``.
    """
    if on_chunk is None or os.environ.get("WORKFLOW_NODE_LOG", "1") == "0":
        return await communicate_or_kill(proc, timeout)

    bufs: Dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    pending: Dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    decoders = {s: codecs.getincrementaldecoder("utf-8")(errors="replace") for s in bufs}
    state = {"emitted": 0, "seq": 0, "truncated": False}

    def emit(part: Dict[str, Any]) -> None:
        part["seq_in_node"] = state["seq"]
        state["seq"] += 1
        try:
            on_chunk(part)
        except Exception as exc:  # live log is best-effort, never fails a node
            logger.debug("node_log on_chunk failed: %s", exc)

    def flush(stream: str, final: bool = False) -> None:
        data = bytes(pending[stream])
        pending[stream].clear()
        if state["truncated"] or not (data or final):
            return
        room = NODE_LOG_MAX_BYTES - state["emitted"]
        cut = len(data) > room
        data = data[:room] if cut else data
        state["emitted"] += len(data)
        text = decoders[stream].decode(data, final=final or cut)
        if text:
            emit({"stream": stream, "text": text})
        if cut:
            state["truncated"] = True
            emit({"stream": stream, "text": "", "truncated": True})

    async def reader(stream: str, pipe: Optional[asyncio.StreamReader]) -> None:
        if pipe is None:
            return
        while True:
            chunk = await pipe.read(65536)
            if not chunk:
                return
            bufs[stream] += chunk
            pending[stream] += chunk
            if len(pending[stream]) >= NODE_LOG_FLUSH_BYTES:
                flush(stream)

    async def ticker() -> None:
        while True:
            await asyncio.sleep(NODE_LOG_FLUSH_S)
            for s in pending:
                flush(s)

    _LIVE_PGIDS.add(proc.pid)
    tasks = [
        asyncio.ensure_future(reader("stdout", proc.stdout)),
        asyncio.ensure_future(reader("stderr", proc.stderr)),
    ]
    tick = asyncio.ensure_future(ticker())
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, proc.wait()), timeout=timeout)
        for s in pending:
            flush(s, final=True)
        return bytes(bufs["stdout"]), bytes(bufs["stderr"])
    except BaseException:
        killpg = getattr(os, "killpg", None)
        try:
            if killpg is not None:
                killpg(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
        for t in tasks:  # an escaped grandchild may hold the pipes open
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await proc.wait()
        raise
    finally:
        tick.cancel()
        _LIVE_PGIDS.discard(proc.pid)

# ── Error Classification ─────────────────────────────────────────────────────

FATAL_PATTERNS = [
    "authentication",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "permission denied",
    "access denied",
    "quota exceeded",
    "billing",
    "payment required",
    "credit",
    "insufficient_quota",
]

TRANSIENT_PATTERNS = [
    "timeout",
    "timed out",
    "rate limit",
    "too many requests",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "overloaded",
    "retry",
]


def classify_error(message: str) -> str:
    """Classify an error message as FATAL, TRANSIENT, or UNKNOWN."""
    lower = message.lower()
    for pattern in FATAL_PATTERNS:
        if pattern in lower:
            return "FATAL"
    for pattern in TRANSIENT_PATTERNS:
        if pattern in lower:
            return "TRANSIENT"
    return "UNKNOWN"


# ── Subprocess Failure Formatting ────────────────────────────────────────────

SUBPROCESS_ERROR_MAX_CHARS = 2000


def format_subprocess_failure(
    error: Exception,
    label: str,
) -> tuple[str, Dict[str, Any]]:
    """
    Produce a concise summary of a failed subprocess.
    Returns (user_message, log_fields).
    """
    import subprocess

    stderr = ""
    exit_code = None
    killed = False

    if isinstance(error, subprocess.CalledProcessError):
        stderr = (error.stderr or "").strip() if isinstance(error.stderr, str) else ""
        exit_code = error.returncode
    elif hasattr(error, "returncode"):
        exit_code = getattr(error, "returncode", None)

    raw_message = str(error).strip()

    # Strip "Command '...' returned non-zero exit status N" prefix
    has_prefix = raw_message.startswith("Command '")
    if has_prefix:
        lines = raw_message.split("\n")
        body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
    else:
        body = raw_message

    if stderr:
        diagnostic = stderr
    elif body:
        diagnostic = body
    elif has_prefix:
        diagnostic = "no diagnostic output"
    else:
        diagnostic = "unknown error"

    truncated = (
        diagnostic[-SUBPROCESS_ERROR_MAX_CHARS:] + "\n…[truncated]"
        if len(diagnostic) > SUBPROCESS_ERROR_MAX_CHARS
        else diagnostic
    )

    exit_suffix = f" [exit {exit_code}]" if exit_code is not None else ""
    stderr_tail = stderr[-SUBPROCESS_ERROR_MAX_CHARS:] if len(stderr) > SUBPROCESS_ERROR_MAX_CHARS else stderr

    return (
        f"{label} failed{exit_suffix}: {truncated}",
        {
            "exit_code": exit_code,
            "killed": killed,
            **({"stderr_tail": stderr_tail} if stderr_tail else {}),
        },
    )


# ── Workflow Variable Substitution ──────────────────────────────────────────
# Ports substituteWorkflowVariables() from executor-shared.ts (HIGH 6).

def substitute_workflow_variables(
    prompt: str,
    workflow_id: str = "",
    user_message: str = "",
    artifacts_dir: str = "",
    base_branch: str = "",
    docs_dir: str = "",
    issue_context: Optional[str] = None,
    loop_user_input: Optional[str] = None,
    rejection_reason: Optional[str] = None,
    loop_prev_output: Optional[str] = None,
    escaped_for_bash: bool = False,
) -> tuple[str, bool]:
    """
    Substitute $WORKFLOW_ID, $ARGUMENTS/$USER_MESSAGE, $ARTIFACTS_DIR,
    $BASE_BRANCH, $CONTEXT/$EXTERNAL_CONTEXT/$ISSUE_CONTEXT, $DOCS_DIR,
    $LOOP_USER_INPUT, $REJECTION_REASON, $LOOP_PREV_OUTPUT in a prompt.

    When escaped_for_bash is True, every substituted value is shell-quoted
    (mirrors substitute_node_output_refs) so the result is safe to pass to
    `bash -c` / interpreter `-c` invocations even when the value is
    attacker-controlled (e.g. $USER_MESSAGE from an HTTP caller).

    Returns (substituted_prompt, context_substituted).
    """
    if base_branch == "" and "$BASE_BRANCH" in prompt:
        raise ValueError(
            "No base branch could be resolved. Auto-detection failed and "
            "`worktree.baseBranch` is not set. Set the base branch explicitly."
        )

    resolved_docs_dir = docs_dir or "docs/"

    def _lit(val: str):
        """Return a re.sub replacement function that inserts val as a literal string."""
        if escaped_for_bash:
            quoted = _shell_quote(val) if val else "''"
            return lambda _m: quoted
        return lambda _m: val

    result = prompt
    result = re.sub(r"\$WORKFLOW_ID", _lit(workflow_id), result)
    result = re.sub(r"\$USER_MESSAGE", _lit(user_message), result)
    result = re.sub(r"\$ARGUMENTS", _lit(user_message), result)
    result = re.sub(r"\$ARTIFACTS_DIR", _lit(artifacts_dir), result)
    result = re.sub(r"\$BASE_BRANCH", _lit(base_branch), result)
    result = re.sub(r"\$DOCS_DIR", _lit(resolved_docs_dir), result)
    result = re.sub(r"\$LOOP_USER_INPUT", _lit(loop_user_input or ""), result)
    result = re.sub(r"\$REJECTION_REASON", _lit(rejection_reason or ""), result)
    result = re.sub(r"\$LOOP_PREV_OUTPUT", _lit(loop_prev_output or ""), result)

    context_substituted = False
    if issue_context is not None:
        result = re.sub(r"\$CONTEXT", _lit(issue_context), result)
        result = re.sub(r"\$EXTERNAL_CONTEXT", _lit(issue_context), result)
        result = re.sub(r"\$ISSUE_CONTEXT", _lit(issue_context), result)
        context_substituted = bool(issue_context)
    else:
        # Replace with empty string to avoid literal $CONTEXT reaching AI
        result = re.sub(r"\$CONTEXT", _lit(""), result)
        result = re.sub(r"\$EXTERNAL_CONTEXT", _lit(""), result)
        result = re.sub(r"\$ISSUE_CONTEXT", _lit(""), result)

    return result, context_substituted


# ── Node Output Ref Substitution ─────────────────────────────────────────────

def _shell_quote(value: str) -> str:
    """Single-quote a string for safe inline shell use."""
    return "'" + value.replace("'", "'\\''") + "'"


def substitute_node_output_refs(
    prompt: str,
    node_outputs: Dict[str, NodeOutput],
    escaped_for_bash: bool = False,
) -> str:
    """
    Substitute $node_id.output and $node_id.output.field references in a prompt.
    Ports substituteNodeOutputRefs from executor-shared.ts.
    """
    def replacer(m: re.Match) -> str:
        node_id = m.group(1)
        field = m.group(2)  # may be None
        node_output = node_outputs.get(node_id)
        if not node_output:
            logger.warning("dag_node_output_ref_unknown_node node_id=%s match=%s", node_id, m.group(0))
            return "''" if escaped_for_bash else ""
        if not field:
            return _shell_quote(node_output.output) if escaped_for_bash else node_output.output
        try:
            parsed = json.loads(node_output.output)
            if not isinstance(parsed, dict):
                return "''" if escaped_for_bash else ""
            value = parsed.get(field)
            if value is None:
                return "''" if escaped_for_bash else ""
            if isinstance(value, str):
                return _shell_quote(value) if escaped_for_bash else value
            if isinstance(value, (int, float, bool)):
                s = str(value).lower() if isinstance(value, bool) else str(value)
                return _shell_quote(s) if escaped_for_bash else s
            if isinstance(value, (list, dict)):
                j = json.dumps(value)
                return _shell_quote(j) if escaped_for_bash else j
            return "''" if escaped_for_bash else ""
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "dag_node_output_ref_json_failed node_id=%s field=%s", node_id, field
            )
            return "''" if escaped_for_bash else ""

    return NODE_OUTPUT_REF_RE.sub(replacer, prompt)
