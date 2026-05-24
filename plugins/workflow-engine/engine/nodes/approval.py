"""
Approval node executor — pauses the workflow for human review.

Ports executeApprovalNode() from dag-executor.ts.

Flow:
1. Render approval message (substitute $nodeId.output refs).
2. Send message to user with approve/reject instructions.
3. Emit approval_requested event.
4. Call ctx.pause_run() — sets run status = 'paused'.
5. Return completed — the between-layer status check sees 'paused' and
   breaks the layer loop, leaving the DAG suspended until resume.

Resume is handled by the runner (Phase 2c): on /approve the runner
sets status back to 'running' and re-calls execute_dag() with
prior_completed populated.
"""

from __future__ import annotations

import logging
from typing import Dict

from engine.schemas.workflow_run import NodeOutput
from engine.core.executor_shared import substitute_node_output_refs

logger = logging.getLogger("workflow.nodes.approval")


async def execute_approval_node(node, node_outputs: Dict[str, NodeOutput], ctx) -> "NodeExecutionResult":
    """Pause the workflow at an approval gate."""
    from engine.core.dag_executor import NodeExecutionResult

    ctx.emit_event("node_started", {
        "run_id": ctx.run_id,
        "node_id": node.id,
        "node_type": "approval",
    })

    rendered_message = substitute_node_output_refs(node.approval.message, node_outputs)
    approval_msg = (
        f"⏸ **Approval required**: {rendered_message}\n\n"
        f"Run ID: `{ctx.run_id}`\n"
        f"Approve: `/workflow approve {ctx.run_id}` | Reject: `/workflow reject {ctx.run_id}`"
    )

    try:
        await ctx.send_message(approval_msg)
    except Exception as exc:
        logger.warning("approval_node.send_message_failed node=%s error=%s", node.id, exc)

    ctx.emit_event("approval_requested", {
        "run_id": ctx.run_id,
        "node_id": node.id,
        "message": rendered_message,
    })

    # MEDIUM 10: include capture_response and on_reject fields so runner
    # can implement rejection-resume and response capture (TS parity).
    approval_cfg = node.approval
    capture_response = getattr(approval_cfg, "capture_response", None)
    on_reject_prompt = getattr(approval_cfg, "on_reject", None)

    pause_meta: dict = {
        "type": "approval",
        "node_id": node.id,
        "message": rendered_message,
    }
    if capture_response is not None:
        pause_meta["captureResponse"] = capture_response
    if on_reject_prompt is not None:
        pause_meta["onRejectPrompt"] = on_reject_prompt

    try:
        await ctx.pause_run(pause_meta)
    except Exception as exc:
        logger.error("approval_node.pause_run_failed node=%s error=%s", node.id, exc)

    # Mark this node_run as paused. Without this the per-node status stays
    # 'running' indefinitely — the workflow_run is paused but the node_run
    # row never reflects it because the DAG only emits node_completed /
    # node_failed / node_skipped terminal events.
    ctx.emit_event("node_paused", {
        "run_id": ctx.run_id,
        "node_id": node.id,
        "message": rendered_message,
    })

    # Return completed — the between-layer status check sees 'paused' and halts.
    return NodeExecutionResult(state="completed", output="")
