"""
Workflow YAML validation helpers.
Wraps Pydantic validation and formats errors in a consistent way.
"""
from __future__ import annotations

from typing import Optional
import yaml as yaml_lib
from pydantic import ValidationError

from engine.schemas.workflow import WorkflowDefinition, WorkflowLoadError


def validate_workflow_yaml(
    content: str, filename: str
) -> tuple[WorkflowDefinition | None, WorkflowLoadError | None]:
    """
    Parse and validate a YAML string as a WorkflowDefinition.

    Returns (workflow, None) on success or (None, error) on failure.
    """
    # 1. YAML parse
    try:
        raw = yaml_lib.safe_load(content)
    except yaml_lib.YAMLError as exc:
        return None, WorkflowLoadError(
            filename=filename,
            error=f"YAML parse error: {exc}",
            errorType="parse_error",
        )

    if not isinstance(raw, dict):
        return None, WorkflowLoadError(
            filename=filename,
            error="Workflow YAML must be a mapping at the top level",
            errorType="parse_error",
        )

    # 2. Required field checks (mirrors TS loader early-exit pattern)
    if not isinstance(raw.get("name"), str) or not raw["name"].strip():
        return None, WorkflowLoadError(
            filename=filename,
            error="Missing required field 'name'",
            errorType="validation_error",
        )
    if not isinstance(raw.get("description"), str) or not raw["description"].strip():
        return None, WorkflowLoadError(
            filename=filename,
            error="Missing required field 'description'",
            errorType="validation_error",
        )

    # 3. Reject legacy steps-based workflows
    if isinstance(raw.get("steps"), list) and len(raw["steps"]) > 0:
        return None, WorkflowLoadError(
            filename=filename,
            error=(
                "`steps:` format has been removed. Workflows now use `nodes:` (DAG) "
                "format exclusively."
            ),
            errorType="validation_error",
        )

    # 4. Require nodes:
    if not isinstance(raw.get("nodes"), list) or len(raw["nodes"]) == 0:
        return None, WorkflowLoadError(
            filename=filename,
            error="Workflow must have 'nodes:' configuration",
            errorType="validation_error",
        )

    # 5. Pydantic validation of the whole document
    try:
        workflow = WorkflowDefinition.model_validate(raw)
    except ValidationError as exc:
        return None, WorkflowLoadError(
            filename=filename,
            error=f"Schema validation failed: {exc}",
            errorType="validation_error",
        )

    # 6. Per-node DAG node validation
    _dag_nodes, node_errors = workflow.get_dag_nodes()
    if node_errors:
        return None, WorkflowLoadError(
            filename=filename,
            error=f"DAG node validation failed: {'; '.join(node_errors)}",
            errorType="validation_error",
        )

    return workflow, None
