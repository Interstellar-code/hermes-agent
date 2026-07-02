"""engine.core — DAG executor, node dispatcher, and shared helpers."""

from engine.core.condition_evaluator import evaluate_condition
from engine.core.dag_executor import (
    build_topological_layers,
    execute_dag,
    check_trigger_rule,
    substitute_node_output_refs,
    substitute_subgraph_inputs,
)
from engine.core.node_dispatcher import dispatch_node
from engine.core.executor_shared import classify_error, format_subprocess_failure

__all__ = [
    "evaluate_condition",
    "build_topological_layers",
    "execute_dag",
    "check_trigger_rule",
    "substitute_node_output_refs",
    "substitute_subgraph_inputs",
    "dispatch_node",
    "classify_error",
    "format_subprocess_failure",
]
