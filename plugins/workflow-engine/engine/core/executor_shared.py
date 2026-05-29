"""
Shared helpers for dag_executor.py and node executors.

Ports executor-shared.ts: error classification, subprocess failure formatting,
variable substitution, node output ref substitution.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from engine.schemas.workflow_run import NodeOutput

logger = logging.getLogger("workflow.executor-shared")

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
) -> tuple[str, bool]:
    """
    Substitute $WORKFLOW_ID, $ARGUMENTS/$USER_MESSAGE, $ARTIFACTS_DIR,
    $BASE_BRANCH, $CONTEXT/$EXTERNAL_CONTEXT/$ISSUE_CONTEXT, $DOCS_DIR,
    $LOOP_USER_INPUT, $REJECTION_REASON, $LOOP_PREV_OUTPUT in a prompt.

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

    return re.sub(
        r"\$([a-zA-Z_][a-zA-Z0-9_-]*)\.output(?:\.([a-zA-Z_][a-zA-Z0-9_]*))?",
        replacer,
        prompt,
    )
