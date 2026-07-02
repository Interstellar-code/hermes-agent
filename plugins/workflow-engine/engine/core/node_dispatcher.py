"""
Node dispatcher — routes a DagNode to its type-specific executor.

Ports executor.ts dispatch logic. Each node type gets its own module
under engine/nodes/. This module is the single entry point called by
dag_executor._execute_node_with_retry().
"""

from __future__ import annotations

import logging
import time
from typing import Dict

from engine.schemas.dag_node import (
    is_bash_node,
    is_loop_node,
    is_approval_node,
    is_cancel_node,
    is_script_node,
    is_subgraph_node,
)
from engine.schemas.workflow_run import NodeOutput

logger = logging.getLogger("workflow.node-dispatcher")


async def dispatch_node(node, node_outputs: Dict[str, NodeOutput], ctx) -> "NodeExecutionResult":
    """
    Dispatch node to its type-specific executor.
    Returns NodeExecutionResult (state, output, error).
    """
    from engine.core.dag_executor import NodeExecutionResult
    from engine.core.logger import log_node_start, log_node_complete, log_node_error

    node_start = time.monotonic()

    if is_subgraph_node(node):
        # Subgraph nodes are pre-expanded; should never reach dispatch
        logger.warning("dispatch_node subgraph node reached dispatch (should be pre-expanded) node=%s", node.id)
        return NodeExecutionResult(state="skipped")

    if is_bash_node(node):
        from engine.nodes.bash import execute_bash_node
        result = await execute_bash_node(node, node_outputs, ctx)
    elif is_script_node(node):
        from engine.nodes.script import execute_script_node
        result = await execute_script_node(node, node_outputs, ctx)
    elif is_approval_node(node):
        from engine.nodes.approval import execute_approval_node
        result = await execute_approval_node(node, node_outputs, ctx)
    elif is_cancel_node(node):
        from engine.nodes.cancel import execute_cancel_node
        result = await execute_cancel_node(node, node_outputs, ctx)
    elif is_loop_node(node):
        from engine.nodes.loop import execute_loop_node
        result = await execute_loop_node(node, node_outputs, ctx)
    elif hasattr(node, "command"):
        from engine.nodes.command import execute_command_node
        result = await execute_command_node(node, node_outputs, ctx)
    elif hasattr(node, "prompt"):
        from engine.nodes.prompt import execute_prompt_node
        result = await execute_prompt_node(node, node_outputs, ctx)
    else:
        logger.error("dispatch_node unknown node type node=%s", node.id)
        return NodeExecutionResult(state="failed", error=f"Unknown node type for node '{node.id}'")

    duration_ms = int((time.monotonic() - node_start) * 1000)
    if result.state == "completed":
        log_node_complete(ctx.log_dir, ctx.run_id, node.id, node.id, duration_ms=duration_ms)
    elif result.state == "failed":
        log_node_error(ctx.log_dir, ctx.run_id, node.id, result.error or "")

    return result
