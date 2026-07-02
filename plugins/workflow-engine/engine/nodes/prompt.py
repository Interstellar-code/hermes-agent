"""
Prompt node executor — calls ctx.llm.complete() with templated messages.

ctx.llm.complete() actual signature (from agent/plugin_llm.py):

    def complete(
        self,
        messages: List[Dict[str, Any]],   # OpenAI shape, positional
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
        agent_id: Optional[str] = None,
        profile: Optional[str] = None,
        purpose: Optional[str] = None,
    ) -> PluginLlmCompleteResult   # .text, .usage

NOTE: complete() is synchronous. We run it in a thread executor so the
async DAG loop is not blocked.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from engine.schemas.workflow_run import NodeOutput
from engine.core.executor_shared import substitute_node_output_refs

logger = logging.getLogger("workflow.nodes.prompt")


async def execute_prompt_node(
    node,
    node_outputs: Dict[str, NodeOutput],
    ctx,
    llm: Any = None,  # ctx.llm — injected for testability
) -> "NodeExecutionResult":
    """
    Execute a prompt node. Ports the prompt-node branch of executeDagWorkflow.

    ``llm`` is the PluginLlmFacade exposed as ctx.llm. In Phase 2c the runner
    wires a real llm; in tests a mock is injected.
    """
    from engine.core.dag_executor import NodeExecutionResult

    node_start = time.monotonic()

    ctx.emit_event("node_started", {
        "run_id": ctx.run_id,
        "node_id": node.id,
        "node_type": "prompt",
    })

    # Resolve llm from ctx if not injected
    if llm is None:
        llm = getattr(ctx, "llm", None)

    if llm is None:
        err = f"Prompt node '{node.id}': no ctx.llm available (Phase 2c wires real llm)"
        logger.error("dag_node_failed node=%s error=%s", node.id, err)
        ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": err})
        return NodeExecutionResult(state="failed", error=err)

    # Build messages — substitute $nodeId.output refs in prompt text
    raw_prompt = getattr(node, "prompt", "") or ""
    final_prompt = substitute_node_output_refs(raw_prompt, node_outputs)

    messages: List[Dict[str, Any]] = []
    system_prompt = getattr(node, "system_prompt", None) or getattr(node, "system", None)
    # system prompt is passed via the messages list (OpenAI shape) or as first user message
    messages.append({"role": "user", "content": final_prompt})

    model = getattr(node, "model", None)
    temperature = getattr(node, "temperature", None)
    max_tokens = getattr(node, "max_tokens", None)

    try:
        # complete() is synchronous — run in thread pool
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: llm.complete(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                purpose=f"workflow-node:{node.id}",
            ),
        )
        output_text = result.text or ""
    except Exception as exc:
        err = f"Prompt node '{node.id}' failed: {exc}"
        logger.error("dag_node_failed node=%s error=%s", node.id, exc)
        ctx.emit_event("node_failed", {"run_id": ctx.run_id, "node_id": node.id, "error": err})
        return NodeExecutionResult(state="failed", error=err)

    duration_ms = int((time.monotonic() - node_start) * 1000)
    logger.info("dag_node_completed node=%s duration_ms=%d", node.id, duration_ms)
    ctx.emit_event("node_completed", {
        "run_id": ctx.run_id,
        "node_id": node.id,
        "output": output_text,
        "duration_ms": duration_ms,
        "type": "prompt",
    })
    return NodeExecutionResult(state="completed", output=output_text)
