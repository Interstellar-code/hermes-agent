"""Bash node executor — runs shell scripts via subprocess."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict

from engine.schemas.workflow_run import NodeOutput
from engine.core.executor_shared import substitute_node_output_refs, format_subprocess_failure, substitute_workflow_variables

logger = logging.getLogger("workflow.nodes.bash")

BASH_DEFAULT_TIMEOUT = 300.0  # seconds


async def execute_bash_node(node, node_outputs: Dict[str, NodeOutput], ctx) -> "NodeExecutionResult":
    """
    Execute a bash node. Ports executeBashNode() from dag-executor.ts.

    - Substitutes $node_id.output refs (shell-quoted)
    - Runs via asyncio subprocess with timeout
    - stdout → node output; stderr → warning log
    """
    from engine.core.dag_executor import NodeExecutionResult
    from engine.core.logger import log_node_start

    node_start = time.monotonic()
    log_node_start(ctx.log_dir, ctx.run_id, node.id, "<bash>")

    ctx.emit_event("node_started", {
        "run_id": ctx.run_id,
        "node_id": node.id,
        "node_type": "bash",
    })

    # HIGH 6: Substitute workflow variables ($ARTIFACTS_DIR, $BASE_BRANCH, etc.)
    raw_script = node.bash
    wf_vars = getattr(ctx, "workflow_vars", None) or {}
    raw_script, _ = substitute_workflow_variables(
        raw_script,
        workflow_id=wf_vars.get("workflow_id", ""),
        user_message=wf_vars.get("user_message", ""),
        artifacts_dir=wf_vars.get("artifacts_dir", ""),
        base_branch=wf_vars.get("base_branch", ""),
        docs_dir=wf_vars.get("docs_dir", ""),
        issue_context=wf_vars.get("issue_context"),
    )

    # Substitute $nodeId.output refs (shell-quoted for bash safety)
    final_script = substitute_node_output_refs(raw_script, node_outputs, escaped_for_bash=True)
    # HIGH 7: timeout field is in milliseconds (TS schema); convert to seconds for subprocess
    timeout_raw = getattr(node, "timeout", None)
    timeout = (timeout_raw / 1000.0) if timeout_raw else BASH_DEFAULT_TIMEOUT

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", final_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            err_msg = f"Bash node '{node.id}' timed out after {timeout}s"
            logger.error("dag_node_failed node=%s type=bash error=%s", node.id, err_msg)
            ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": err_msg})
            return NodeExecutionResult(state="failed", error=err_msg)

        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            err_msg = f"Bash node '{node.id}' exited with code {proc.returncode}"
            if stderr:
                err_msg += f": {stderr[-2000:]}"
            logger.error("dag_node_failed node=%s type=bash exit=%d", node.id, proc.returncode)
            ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": err_msg})
            return NodeExecutionResult(state="failed", error=err_msg)

        if stderr:
            logger.warning("dag_node_bash_stderr node=%s stderr=%.500s", node.id, stderr)
            try:
                await ctx.send_message(f"Bash node '{node.id}' stderr:\n```\n{stderr}\n```")
            except Exception:
                pass

        duration_ms = int((time.monotonic() - node_start) * 1000)
        logger.info("dag_node_completed node=%s duration_ms=%d", node.id, duration_ms)
        ctx.emit_event("node_completed", {
            "run_id": ctx.run_id,
            "node_id": node.id,
            "output": stdout,
            "duration_ms": duration_ms,
            "type": "bash",
        })
        return NodeExecutionResult(state="completed", output=stdout)

    except FileNotFoundError:
        err_msg = f"Bash node '{node.id}' failed: bash executable not found in PATH"
        logger.error("dag_node_failed node=%s error=%s", node.id, err_msg)
        ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": err_msg})
        return NodeExecutionResult(state="failed", error=err_msg)
    except Exception as exc:
        user_msg, log_fields = format_subprocess_failure(exc, f"Bash node '{node.id}'")
        logger.error("dag_node_failed node=%s %s", node.id, log_fields)
        ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": user_msg})
        return NodeExecutionResult(state="failed", error=user_msg)
