"""
Pydantic models for workflow run state types.
Mirrors TS schemas/workflow-run.ts exactly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal, Optional, Union
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Status / state enums
# ---------------------------------------------------------------------------

WorkflowRunStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
    "paused",
]

WorkflowStepStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "skipped",
]

NodeState = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "skipped",
]

TERMINAL_WORKFLOW_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")
RESUMABLE_WORKFLOW_STATUSES: tuple[str, ...] = ("failed", "paused")

# ---------------------------------------------------------------------------
# NodeOutput — discriminated union on state
# ---------------------------------------------------------------------------

class NodeOutputCompleted(BaseModel):
    state: Literal["completed", "running"]
    output: str
    sessionId: Optional[str] = None


class NodeOutputFailed(BaseModel):
    state: Literal["failed"]
    output: str
    sessionId: Optional[str] = None
    error: str


class NodeOutputPending(BaseModel):
    state: Literal["pending", "skipped"]
    output: str


NodeOutput = Union[NodeOutputCompleted, NodeOutputFailed, NodeOutputPending]

# ---------------------------------------------------------------------------
# ArtifactType
# ---------------------------------------------------------------------------

ArtifactType = Literal["pr", "commit", "file_created", "file_modified", "branch"]

# ---------------------------------------------------------------------------
# WorkflowRun
# ---------------------------------------------------------------------------

class WorkflowRun(BaseModel):
    """Runtime workflow run state stored in database."""

    id: str
    workflow_name: str
    conversation_id: str
    parent_conversation_id: Optional[str] = None
    codebase_id: Optional[str] = None
    status: WorkflowRunStatus
    user_message: str
    metadata: Dict[str, Any]
    started_at: datetime
    completed_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    working_path: Optional[str] = None

# ---------------------------------------------------------------------------
# ApprovalContext
# ---------------------------------------------------------------------------

class ApprovalContext(BaseModel):
    nodeId: str
    message: str
    type: Optional[Literal["approval", "interactive_loop"]] = None
    iteration: Optional[int] = None
    sessionId: Optional[str] = None
    captureResponse: Optional[bool] = None
    onRejectPrompt: Optional[str] = None
    onRejectMaxAttempts: Optional[int] = None


def make_node_output(state: str, output: str = "", error: Optional[str] = None) -> NodeOutput:
    """Factory to construct the correct NodeOutput subclass from a state string."""
    if state in ("completed", "running"):
        return NodeOutputCompleted(state=state, output=output)  # type: ignore[arg-type]
    if state == "failed":
        return NodeOutputFailed(state="failed", output=output, error=error or "")
    # pending, skipped
    return NodeOutputPending(state=state, output=output)  # type: ignore[arg-type]


def is_approval_context(val: Any) -> bool:
    """Type guard for ApprovalContext."""
    return (
        isinstance(val, dict)
        and isinstance(val.get("nodeId"), str)
        and isinstance(val.get("message"), str)
    )
