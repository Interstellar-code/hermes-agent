"""
Command node executor — routes through ctx.llm as a prompt invocation.

Ports executeCommandNode() from dag-executor.ts (BLOCKER 2).
The TS implementation treats command nodes as "slash-command-style" LLM
prompt invocations, NOT shell executions. The command name + args are
formatted into a prompt and sent to the LLM provider.

If ctx.llm is unavailable (test stubs), falls back to returning the
formatted prompt as output (same pattern as loop node test mode).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict

from engine.schemas.workflow_run import NodeOutput
from engine.core.executor_shared import substitute_node_output_refs

logger = logging.getLogger("workflow.nodes.command")

COMMAND_DEFAULT_TIMEOUT = 300.0


async def execute_command_node(node, node_outputs: Dict[str, NodeOutput], ctx) -> "NodeExecutionResult":
    """Execute a command node via ctx.llm (LLM prompt invocation, not shell)."""
    from engine.core.dag_executor import NodeExecutionResult

    node_start = time.monotonic()
    ctx.emit_event("node_started", {
        "run_id": ctx.run_id,
        "node_id": node.id,
        "node_type": "command",
    })

    # Substitute $nodeId.output refs in the command string
    final_command = substitute_node_output_refs(node.command, node_outputs)

    # Format as a slash-command-style prompt (mirrors TS: "/" + command)
    prompt = f"/{final_command}"

    llm = getattr(ctx, "llm", None)
    timeout_raw = getattr(node, "timeout", None)
    # HIGH 7: timeout is in ms — convert to seconds
    timeout = (timeout_raw / 1000.0) if timeout_raw else COMMAND_DEFAULT_TIMEOUT

    if llm is not None:
        try:
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: llm.complete(
                        [{"role": "user", "content": prompt}],
                        purpose=f"workflow-command:{node.id}",
                    ),
                ),
                timeout=timeout,
            )
            output = result.text or ""
        except asyncio.TimeoutError:
            err = f"Command node '{node.id}' timed out after {timeout}s"
            logger.error("dag_node_failed node=%s type=command error=%s", node.id, err)
            ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": err})
            return NodeExecutionResult(state="failed", error=err)
        except Exception as exc:
            err = f"Command node '{node.id}' LLM call failed: {exc}"
            logger.error("dag_node_failed node=%s type=command error=%s", node.id, exc)
            ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": err})
            return NodeExecutionResult(state="failed", error=err)
    else:
        # No LLM available (test mode) — return formatted prompt as output
        logger.warning("command_node.no_llm node=%s — returning prompt as output", node.id)
        output = prompt

    duration_ms = int((time.monotonic() - node_start) * 1000)
    ctx.emit_event("node_completed", {
        "run_id": ctx.run_id, "node_id": node.id,
        "output": output, "duration_ms": duration_ms, "type": "command",
    })
    return NodeExecutionResult(state="completed", output=output)
