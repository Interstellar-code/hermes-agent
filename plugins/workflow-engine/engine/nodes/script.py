"""
Script node executor — runs inline Python (via uv) or TypeScript/JS (via bun).

Ports executeScriptNode() from dag-executor.ts.
Fixes (Phase 2b parity):
- HIGH 6: substitute_workflow_variables before exec
- HIGH 7: timeout field is milliseconds → divide by 1000 for subprocess
- HIGH 8: inline-vs-named detection, .archon/scripts discovery, deps install, runtime args
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from engine.schemas.workflow_run import NodeOutput
from engine.core.executor_shared import (
    substitute_node_output_refs,
    format_subprocess_failure,
    substitute_workflow_variables,
)

logger = logging.getLogger("workflow.nodes.script")

SCRIPT_DEFAULT_TIMEOUT = 120.0  # seconds


def _is_inline_script(script: str) -> bool:
    """
    Ports isInlineScript() from executor-shared.ts.
    A script is inline if it contains a newline or any shell-like special char.
    Named scripts are bare identifiers like 'fetch-data' or 'my-script'.
    """
    return "\n" in script or bool(re.search(r'[;(){}&|<>$`"\' ]', script))


def _discover_named_script(name: str, cwd: Optional[str]) -> Optional[str]:
    """
    Look up a named script across repo and home scopes.
    Precedence: <cwd>/.archon/scripts/ > ~/.archon/scripts/ (repo wins).
    Matches any extension (e.g. fetch-data.py, fetch-data.ts).
    Returns the absolute path or None.
    """
    search_dirs: List[Path] = []
    if cwd:
        search_dirs.append(Path(cwd) / ".archon" / "scripts")
    home_dir = Path.home() / ".archon" / "scripts"
    search_dirs.append(home_dir)

    for scripts_dir in search_dirs:
        if not scripts_dir.is_dir():
            continue
        # Exact name match first (name already has extension)
        exact = scripts_dir / name
        if exact.is_file():
            return str(exact)
        # Try common extensions
        for ext in (".py", ".ts", ".js", ".sh"):
            candidate = scripts_dir / (name + ext)
            if candidate.is_file():
                return str(candidate)

    return None


async def execute_script_node(node, node_outputs: Dict[str, NodeOutput], ctx) -> "NodeExecutionResult":
    """Execute a script node (uv for Python, bun for TypeScript)."""
    from engine.core.dag_executor import NodeExecutionResult

    node_start = time.monotonic()
    runtime = node.runtime  # "uv" | "bun"

    ctx.emit_event("node_started", {
        "run_id": ctx.run_id,
        "node_id": node.id,
        "node_type": "script",
        "runtime": runtime,
    })

    # HIGH 7: timeout is in milliseconds — convert to seconds for subprocess
    timeout_raw = getattr(node, "timeout", None)
    timeout = (timeout_raw / 1000.0) if timeout_raw else SCRIPT_DEFAULT_TIMEOUT

    # HIGH 6: Substitute workflow variables ($ARTIFACTS_DIR, $BASE_BRANCH, etc.)
    raw_script = node.script
    wf_vars = getattr(ctx, "workflow_vars", None) or {}
    try:
        raw_script, _ = substitute_workflow_variables(
            raw_script,
            workflow_id=wf_vars.get("workflow_id", ""),
            user_message=wf_vars.get("user_message", ""),
            artifacts_dir=wf_vars.get("artifacts_dir", ""),
            base_branch=wf_vars.get("base_branch", ""),
            docs_dir=wf_vars.get("docs_dir", ""),
            issue_context=wf_vars.get("issue_context"),
        )
    except ValueError as exc:
        err = f"Script node '{node.id}' variable substitution failed: {exc}"
        ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": err})
        return NodeExecutionResult(state="failed", error=err)

    # Substitute $nodeId.output refs
    final_script = substitute_node_output_refs(raw_script, node_outputs)

    # HIGH 8: inline-vs-named detection
    cwd = getattr(ctx, "cwd", None)
    node_deps: List[str] = list(getattr(node, "deps", None) or [])

    if not _is_inline_script(final_script):
        # Named script: discover from .archon/scripts/
        script_path = _discover_named_script(final_script.strip(), cwd)
        if script_path is None:
            err = (
                f"Script node '{node.id}': named script '{final_script.strip()}' not found "
                f"in .archon/scripts/ or ~/.archon/scripts/"
            )
            ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": err})
            return NodeExecutionResult(state="failed", error=err)
        # Build command for named script
        if runtime == "bun":
            cmd_parts = ["bun", "--no-env-file", "run", script_path]
        else:
            with_flags = [flag for dep in node_deps for flag in ("--with", dep)]
            cmd_parts = ["uv", "run"] + with_flags + [script_path]
        use_temp = False
    else:
        # Inline code: pass directly to interpreter
        if runtime == "bun":
            cmd_parts = ["bun", "--no-env-file", "-e", final_script]
        else:
            with_flags = [flag for dep in node_deps for flag in ("--with", dep)]
            cmd_parts = ["uv", "run"] + with_flags + ["python", "-c", final_script]
        use_temp = False

    try:

        exec_env = dict(os.environ)
        if wf_vars.get("artifacts_dir"):
            exec_env["ARTIFACTS_DIR"] = wf_vars["artifacts_dir"]

        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=exec_env,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            err = f"Script node '{node.id}' timed out after {timeout}s"
            ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": err})
            return NodeExecutionResult(state="failed", error=err)
        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            err = f"Script node '{node.id}' exited with code {proc.returncode}"
            if stderr:
                err += f": {stderr[-2000:]}"
            ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": err})
            return NodeExecutionResult(state="failed", error=err)

        if stderr:
            logger.warning("dag_node_script_stderr node=%s stderr=%.500s", node.id, stderr)

        duration_ms = int((time.monotonic() - node_start) * 1000)
        ctx.emit_event("node_completed", {
            "run_id": ctx.run_id, "node_id": node.id,
            "output": stdout, "duration_ms": duration_ms, "type": "script",
        })
        return NodeExecutionResult(state="completed", output=stdout)

    except FileNotFoundError:
        err = f"Script node '{node.id}' failed: interpreter '{runtime}' not found in PATH"
        ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": err})
        return NodeExecutionResult(state="failed", error=err)
    except Exception as exc:
        user_msg, _ = format_subprocess_failure(exc, f"Script node '{node.id}'")
        ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": user_msg})
        return NodeExecutionResult(state="failed", error=user_msg)
