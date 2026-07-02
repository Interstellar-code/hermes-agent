"""
Subgraph node executor.

In practice subgraph nodes are pre-expanded by execute_dag() before the
layer loop runs — they never reach dispatch_node(). This module exists
as a safety net and to satisfy the "one module per node type" spec.
"""

from __future__ import annotations

import logging
from typing import Dict

from engine.schemas.workflow_run import NodeOutput

logger = logging.getLogger("workflow.nodes.subgraph")


async def execute_subgraph_node(node, node_outputs: Dict[str, NodeOutput], ctx) -> "NodeExecutionResult":
    """Subgraph nodes are pre-expanded; this path should never be reached."""
    from engine.core.dag_executor import NodeExecutionResult

    logger.warning(
        "subgraph_node_reached_dispatch node=%s — should have been pre-expanded", node.id
    )
    return NodeExecutionResult(state="skipped", output="")
