"""
Pydantic models for per-node hook configuration.
Mirrors TS schemas/hooks.ts exactly.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field

# Supported hook events — mirrors workflowHookEventSchema in hooks.ts
WORKFLOW_HOOK_EVENTS = [
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Notification",
    "UserPromptSubmit",
    "SessionStart",
    "SessionEnd",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PermissionRequest",
    "Setup",
    "TeammateIdle",
    "TaskCompleted",
    "Elicitation",
    "ElicitationResult",
    "ConfigChange",
    "WorktreeCreate",
    "WorktreeRemove",
    "InstructionsLoaded",
]

WorkflowHookEvent = Literal[
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Notification",
    "UserPromptSubmit",
    "SessionStart",
    "SessionEnd",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PermissionRequest",
    "Setup",
    "TeammateIdle",
    "TaskCompleted",
    "Elicitation",
    "ElicitationResult",
    "ConfigChange",
    "WorktreeCreate",
    "WorktreeRemove",
    "InstructionsLoaded",
]


class WorkflowHookMatcher(BaseModel):
    """A single hook matcher in a YAML workflow definition."""

    matcher: Optional[str] = Field(
        default=None,
        description="Regex pattern to match tool names or event subtypes.",
    )
    response: Dict[str, Any] = Field(
        ...,
        description="The SDK SyncHookJSONOutput to return when this hook fires.",
    )
    timeout: Optional[float] = Field(
        default=None,
        gt=0,
        description="Timeout in seconds (default: SDK default of 60).",
    )


class WorkflowNodeHooks(BaseModel):
    """Per-node hook configuration keyed by event name. Mirrors workflowNodeHooksSchema (strict)."""

    model_config = {"extra": "forbid"}

    PreToolUse: Optional[List[WorkflowHookMatcher]] = None
    PostToolUse: Optional[List[WorkflowHookMatcher]] = None
    PostToolUseFailure: Optional[List[WorkflowHookMatcher]] = None
    Notification: Optional[List[WorkflowHookMatcher]] = None
    UserPromptSubmit: Optional[List[WorkflowHookMatcher]] = None
    SessionStart: Optional[List[WorkflowHookMatcher]] = None
    SessionEnd: Optional[List[WorkflowHookMatcher]] = None
    Stop: Optional[List[WorkflowHookMatcher]] = None
    SubagentStart: Optional[List[WorkflowHookMatcher]] = None
    SubagentStop: Optional[List[WorkflowHookMatcher]] = None
    PreCompact: Optional[List[WorkflowHookMatcher]] = None
    PermissionRequest: Optional[List[WorkflowHookMatcher]] = None
    Setup: Optional[List[WorkflowHookMatcher]] = None
    TeammateIdle: Optional[List[WorkflowHookMatcher]] = None
    TaskCompleted: Optional[List[WorkflowHookMatcher]] = None
    Elicitation: Optional[List[WorkflowHookMatcher]] = None
    ElicitationResult: Optional[List[WorkflowHookMatcher]] = None
    ConfigChange: Optional[List[WorkflowHookMatcher]] = None
    WorktreeCreate: Optional[List[WorkflowHookMatcher]] = None
    WorktreeRemove: Optional[List[WorkflowHookMatcher]] = None
    InstructionsLoaded: Optional[List[WorkflowHookMatcher]] = None
