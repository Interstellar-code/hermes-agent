# engine.discovery package
from dataclasses import dataclass
from typing import Optional, Any

from engine.discovery.loader import parse_workflow, discover_and_upsert
from engine.schemas.workflow import WorkflowLoadResult, WorkflowLoadError, WorkflowDefinition


@dataclass
class _ParseResult:
    """Simple result container for parse_workflow_yaml used by dag_executor."""
    workflow: Optional[Any]
    error: Optional[Any]


def parse_workflow_yaml(yaml_content: str, filename: str = "<inline>") -> "_ParseResult":
    """
    Parse and validate a workflow YAML string.
    Returns _ParseResult(workflow=..., error=None) on success
    or _ParseResult(workflow=None, error=...) on failure.

    Used by dag_executor._expand_subgraph().
    """
    workflow, error = parse_workflow(yaml_content, filename)
    return _ParseResult(workflow=workflow, error=error)


__all__ = [
    "parse_workflow",
    "parse_workflow_yaml",
    "discover_and_upsert",
]
