"""
Pydantic models for workflow definition types.
Mirrors TS schemas/workflow.ts exactly.
"""
from __future__ import annotations

import re
from typing import Any, List, Literal, Optional, Union
from pydantic import BaseModel, Field

from engine.schemas.dag_node import (
    DagNodeBase,
    EffortLevel,
    SandboxSettings,
    validate_dag_node,
    DagNode,
)

# ---------------------------------------------------------------------------
# Shared enum types
# ---------------------------------------------------------------------------

ModelReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh"]

WebSearchMode = Literal["disabled", "cached", "live"]

# ---------------------------------------------------------------------------
# Subgraph input/output declarations (A.7-subgraphs)
# ---------------------------------------------------------------------------

_SNAKE_CASE_REGEX = re.compile(r"^[a-z][a-z0-9_]*$", re.IGNORECASE)


class SubgraphInput(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    type: Optional[Literal["string", "number", "boolean", "object", "array"]] = None
    required: Optional[bool] = None
    description: Optional[str] = None


class SubgraphOutput(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    from_: str = Field(..., alias="from", min_length=1)
    description: Optional[str] = None

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# WorkflowWorktreePolicy
# ---------------------------------------------------------------------------

class WorkflowWorktreePolicy(BaseModel):
    enabled: Optional[bool] = None


# ---------------------------------------------------------------------------
# WorkflowBase — common fields shared by all workflow types
# ---------------------------------------------------------------------------

class WorkflowBase(BaseModel):
    """Common fields shared by all workflow definitions."""

    model_config = {"extra": "allow"}

    kind: Optional[Literal["workflow", "subgraph"]] = None
    id: Optional[str] = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    inputs: Optional[List[SubgraphInput]] = None
    outputs: Optional[List[SubgraphOutput]] = None
    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    provider: Optional[str] = Field(default=None, min_length=1)
    model: Optional[str] = None
    modelReasoningEffort: Optional[ModelReasoningEffort] = None
    webSearchMode: Optional[WebSearchMode] = None
    additionalDirectories: Optional[List[str]] = None
    interactive: Optional[bool] = None
    effort: Optional[EffortLevel] = None
    thinking: Optional[Any] = None
    fallbackModel: Optional[str] = Field(default=None, min_length=1)
    betas: Optional[List[str]] = None
    sandbox: Optional[SandboxSettings] = None
    worktree: Optional[WorkflowWorktreePolicy] = None
    mutates_checkout: Optional[bool] = None
    tags: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# WorkflowDefinition — DAG-based workflow with nodes
# ---------------------------------------------------------------------------

class WorkflowDefinition(WorkflowBase):
    """Workflow definition parsed from YAML. All workflows use nodes: (DAG) format."""

    nodes: List[Any]  # validated by validate_dag_node per-node in discovery layer

    def get_dag_nodes(self) -> tuple[list[DagNode], list[str]]:
        """
        Validate and return typed DagNode list.
        Returns (nodes, errors). On partial failure errors is non-empty.
        """
        dag_nodes: list[DagNode] = []
        errors: list[str] = []
        for i, raw in enumerate(self.nodes):
            node, node_errors = validate_dag_node(raw, i)
            if node is not None:
                dag_nodes.append(node)
            errors.extend(node_errors)
        return dag_nodes, errors


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

WorkflowSource = Literal["bundled", "global", "project"]


class WorkflowWithSource(BaseModel):
    workflow: WorkflowDefinition
    source: WorkflowSource


class WorkflowLoadError(BaseModel):
    filename: str
    error: str
    errorType: Literal["read_error", "parse_error", "validation_error"]


class WorkflowLoadResult(BaseModel):
    workflows: List[WorkflowWithSource]
    errors: List[WorkflowLoadError]
