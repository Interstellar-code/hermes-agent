"""
Loop node executor — iterates a prompt until a completion signal or max iterations.

Ports executeLoopNode() from dag-executor.ts (HIGH 9).
Supports:
- loop.over: list iteration (one iteration per item, $LOOP_ITEM substituted)
- loop.until: completion signal string detected in AI output
- loop.until_bash: bash script run after each iteration; exit 0 = complete
- loop.max_iterations: max bound; failure raised if until never satisfied
- loop.fresh_context: start fresh LLM session each iteration (noted in ctx call)
- loop.interactive: pause between iterations via ctx.pause_run (Phase 2c full impl)
  — in Phase 2b: gate message is sent and run paused; resume requires runner support
- Fail on max-iter exhaustion when until is set and never satisfied
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from engine.schemas.workflow_run import NodeOutput
from engine.core.executor_shared import substitute_node_output_refs, substitute_workflow_variables

logger = logging.getLogger("workflow.nodes.loop")


async def _run_until_bash(script: str, cwd: Optional[str] = None) -> bool:
    """Run until_bash script; returns True if exit code == 0 (complete)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            logger.warning("loop_node.until_bash_timeout script timed out after 30s")
            return False
        return proc.returncode == 0
    except Exception as exc:
        logger.warning("loop_node.until_bash_exec_error error=%s", exc)
        return False


async def execute_loop_node(node, node_outputs: Dict[str, NodeOutput], ctx) -> "NodeExecutionResult":
    """
    Execute a loop node.

    - loop.over: iterate over items (one per item, $LOOP_ITEM substituted)
    - loop.until: completion signal string; detected in AI output each iteration
    - loop.until_bash: bash exit-0 as alternative completion check
    - loop.max_iterations: hard cap; failure raised if until never satisfied
    - loop.fresh_context: hint to start fresh session (passed to llm.complete purpose)
    - loop.interactive: pause between iterations for user input
    """
    from engine.core.dag_executor import NodeExecutionResult

    loop_cfg = node.loop
    node_start = time.monotonic()

    ctx.emit_event("node_started", {
        "run_id": ctx.run_id,
        "node_id": node.id,
        "node_type": "loop",
    })

    over_items: Optional[List[Any]] = getattr(loop_cfg, "over", None)
    max_iterations: int = getattr(loop_cfg, "max_iterations", 1)
    prompt_template: str = getattr(loop_cfg, "prompt", "") or ""
    until_signal: Optional[str] = getattr(loop_cfg, "until", None)
    until_bash: Optional[str] = getattr(loop_cfg, "until_bash", None)
    fresh_context: bool = getattr(loop_cfg, "fresh_context", False)
    interactive: bool = bool(getattr(loop_cfg, "interactive", False))
    gate_message: Optional[str] = getattr(loop_cfg, "gate_message", None)

    # Determine iteration set
    if over_items is not None:
        items: List[Any] = list(over_items)
        # For list loops, don't enforce until-exhaustion failure
        is_ai_loop = False
    else:
        items = list(range(max_iterations))
        # AI loops require until to complete successfully
        is_ai_loop = until_signal is not None

    if not items:
        ctx.emit_event("node_completed", {
            "run_id": ctx.run_id, "node_id": node.id,
            "output": "", "type": "loop",
        })
        return NodeExecutionResult(state="completed", output="")

    llm = getattr(ctx, "llm", None)
    cwd = getattr(ctx, "cwd", None)
    last_output = ""
    completion_detected = False

    for i, item in enumerate(items):
        iter_start = time.monotonic()
        ctx.emit_event("loop_iteration_started", {
            "run_id": ctx.run_id,
            "node_id": node.id,
            "iteration": i + 1,
        })

        # Substitute refs and loop variables
        prompt = substitute_node_output_refs(prompt_template, node_outputs)
        item_str = str(item) if not isinstance(item, str) else item
        prompt = prompt.replace("$LOOP_ITEM", item_str)
        prompt = prompt.replace("$LOOP_INDEX", str(i))
        prompt = prompt.replace("$LOOP_PREV_OUTPUT", last_output)

        if llm is not None:
            try:
                loop_ev = asyncio.get_event_loop()
                # fresh_context: use distinct purpose so provider may start fresh session
                purpose_suffix = f":fresh" if (fresh_context and i > 0) else ""
                result = await loop_ev.run_in_executor(
                    None,
                    lambda p=prompt, s=purpose_suffix: llm.complete(
                        [{"role": "user", "content": p}],
                        purpose=f"workflow-loop:{node.id}:iter{i + 1}{s}",
                    ),
                )
                iteration_output = result.text or ""
            except Exception as exc:
                err = f"Loop node '{node.id}' iteration {i + 1} failed: {exc}"
                logger.error("loop_node.iteration_failed node=%s iter=%d error=%s", node.id, i + 1, exc)
                ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": err})
                return NodeExecutionResult(state="failed", error=err)
        else:
            # No LLM — test mode: return prompt as output
            iteration_output = prompt

        last_output = iteration_output
        iter_duration_ms = int((time.monotonic() - iter_start) * 1000)
        ctx.emit_event("loop_iteration_completed", {
            "run_id": ctx.run_id,
            "node_id": node.id,
            "iteration": i + 1,
            "output": iteration_output,
            "duration_ms": iter_duration_ms,
        })

        # Check completion signal in AI output
        signal_in_output = until_signal and until_signal in iteration_output
        bash_complete = False
        if until_bash:
            wf_vars = getattr(ctx, "workflow_vars", None) or {}
            until_bash_script, _ = substitute_workflow_variables(
                until_bash,
                workflow_id=wf_vars.get("workflow_id", ""),
                user_message=wf_vars.get("user_message", ""),
                artifacts_dir=wf_vars.get("artifacts_dir", ""),
                base_branch=wf_vars.get("base_branch", ""),
                docs_dir=wf_vars.get("docs_dir", ""),
                issue_context=wf_vars.get("issue_context"),
                escaped_for_bash=True,
            )
            until_bash_script = substitute_node_output_refs(until_bash_script, node_outputs, escaped_for_bash=True)
            bash_complete = await _run_until_bash(until_bash_script, cwd)

        completion_detected = bool(signal_in_output or bash_complete)

        if completion_detected:
            logger.info(
                "loop_node.completion_detected node=%s iter=%d signal=%s bash=%s",
                node.id, i + 1, signal_in_output, bash_complete,
            )
            break

        # Interactive loop: pause between iterations for user input
        if interactive and gate_message and i < len(items) - 1:
            rendered = gate_message.replace("$LOOP_INDEX", str(i)).replace("$LOOP_PREV_OUTPUT", last_output)
            pause_msg = (
                f"⏸ **Loop gate**: {rendered}\n\n"
                f"Run ID: `{ctx.run_id}`\n"
                f"Continue: `/workflow approve {ctx.run_id}` | Stop: `/workflow reject {ctx.run_id}`"
            )
            try:
                await ctx.send_message(pause_msg)
            except Exception:
                pass
            ctx.emit_event("approval_requested", {
                "run_id": ctx.run_id,
                "node_id": node.id,
                "message": rendered,
                "type": "interactive_loop",
                "iteration": i + 1,
            })
            try:
                await ctx.pause_run({
                    "type": "interactive_loop",
                    "node_id": node.id,
                    "message": rendered,
                    "iteration": i + 1,
                })
            except Exception as exc:
                logger.error("loop_node.pause_run_failed node=%s error=%s", node.id, exc)
            # Return completed — between-layer status check sees 'paused' and halts
            return NodeExecutionResult(state="completed", output=last_output)

    # HIGH 9: Fail if AI loop exhausted max_iterations without satisfying until
    if is_ai_loop and not completion_detected:
        err = (
            f"Loop node '{node.id}' exceeded max iterations ({max_iterations}) "
            f"without completion signal '{until_signal}'"
        )
        logger.error("loop_node.max_iterations_exceeded node=%s max=%d signal=%s", node.id, max_iterations, until_signal)
        ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": err})
        return NodeExecutionResult(state="failed", error=err)

    duration_ms = int((time.monotonic() - node_start) * 1000)
    ctx.emit_event("node_completed", {
        "run_id": ctx.run_id, "node_id": node.id,
        "output": last_output, "duration_ms": duration_ms, "type": "loop",
    })
    return NodeExecutionResult(state="completed", output=last_output)
