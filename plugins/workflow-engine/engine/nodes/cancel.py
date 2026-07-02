"""
Cancel node executor — cancels the workflow run with a reason message.

Ports the cancel-node branch of the DAG executor layer loop in dag-executor.ts.
"""

from __future__ import annotations

import logging
from typing import Dict

from engine.schemas.workflow_run import NodeOutput
from engine.core.executor_shared import substitute_node_output_refs

logger = logging.getLogger("workflow.nodes.cancel")


async def execute_cancel_node(node, node_outputs: Dict[str, NodeOutput], ctx) -> "NodeExecutionResult":
    """
    Cancel the workflow run.

    Substitutes $nodeId.output refs in the cancel message, emits
    workflow_cancelled, calls ctx.cancel_run(), then returns completed
    so the between-layer status check sees 'cancelled' and breaks.
    """
    from engine.core.dag_executor import NodeExecutionResult

    ctx.emit_event("node_started", {
        "run_id": ctx.run_id,
        "node_id": node.id,
        "node_type": "cancel",
    })

    reason = substitute_node_output_refs(node.cancel, node_outputs)
    cancel_msg = f"❌ **Workflow cancelled** (node `{node.id}`): {reason}"

    try:
        await ctx.send_message(cancel_msg)
    except Exception:
        pass

    ctx.emit_event("workflow_cancelled", {
        "run_id": ctx.run_id,
        "node_id": node.id,
        "reason": reason,
    })

    try:
        await ctx.cancel_run()
    except Exception as exc:
        logger.error("cancel_node.cancel_run_failed node=%s error=%s", node.id, exc)

    # Return completed — the between-layer status check will see 'cancelled' and break.
    return NodeExecutionResult(state="completed", output=reason)
