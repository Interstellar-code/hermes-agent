"""
Pydantic models for DAG node types.
Mirrors TS schemas/dag-node.ts exactly.

Design: each node type is a separate model with discriminated presence of its
mode field (command/prompt/bash/loop/approval/cancel/script/subgraph).
validate_dag_node() enforces mutual exclusivity and other cross-field rules.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, model_validator

from engine.schemas.retry import StepRetryConfig
from engine.schemas.loop import LoopNodeConfig
from engine.schemas.hooks import WorkflowNodeHooks

# ---------------------------------------------------------------------------
# TriggerRule
# ---------------------------------------------------------------------------

TriggerRule = Literal[
    "all_success",
    "one_success",
    "none_failed_min_one_success",
    "all_done",
]

TRIGGER_RULES: tuple[str, ...] = (
    "all_success",
    "one_success",
    "none_failed_min_one_success",
    "all_done",
)

# ---------------------------------------------------------------------------
# Claude SDK option schemas
# ---------------------------------------------------------------------------

EffortLevel = Literal["low", "medium", "high", "max"]

ModelReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh"]


class ThinkingConfigAdaptive(BaseModel):
    type: Literal["adaptive"]


class ThinkingConfigEnabled(BaseModel):
    type: Literal["enabled"]
    budgetTokens: Optional[int] = Field(default=None, gt=0)


class ThinkingConfigDisabled(BaseModel):
    type: Literal["disabled"]


ThinkingConfig = Union[ThinkingConfigAdaptive, ThinkingConfigEnabled, ThinkingConfigDisabled]


def _parse_thinking_config(val: Any) -> Any:
    """Pre-process string shorthand into dict form (mirrors TS z.preprocess)."""
    if isinstance(val, str):
        if val == "adaptive":
            return {"type": "adaptive"}
        if val == "enabled":
            return {"type": "enabled"}
        if val == "disabled":
            return {"type": "disabled"}
    return val


class SandboxNetworkSettings(BaseModel):
    model_config = {"extra": "allow"}
    allowedDomains: Optional[List[str]] = None
    allowManagedDomainsOnly: Optional[bool] = None
    allowUnixSockets: Optional[List[str]] = None
    allowAllUnixSockets: Optional[bool] = None
    allowLocalBinding: Optional[bool] = None
    httpProxyPort: Optional[float] = None
    socksProxyPort: Optional[float] = None


class SandboxFilesystemSettings(BaseModel):
    model_config = {"extra": "allow"}
    allowWrite: Optional[List[str]] = None
    denyWrite: Optional[List[str]] = None
    denyRead: Optional[List[str]] = None


class SandboxSettings(BaseModel):
    model_config = {"extra": "allow"}
    enabled: Optional[bool] = None
    autoAllowBashIfSandboxed: Optional[bool] = None
    allowUnsandboxedCommands: Optional[bool] = None
    network: Optional[SandboxNetworkSettings] = None
    filesystem: Optional[SandboxFilesystemSettings] = None
    ignoreViolations: Optional[Dict[str, List[str]]] = None
    enableWeakerNestedSandbox: Optional[bool] = None
    enableWeakerNetworkIsolation: Optional[bool] = None
    excludedCommands: Optional[List[str]] = None
    ripgrep: Optional[Dict[str, Any]] = None


class AgentDefinition(BaseModel):
    description: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    model: Optional[str] = Field(default=None, min_length=1)
    tools: Optional[List[str]] = None
    disallowedTools: Optional[List[str]] = None
    skills: Optional[List[str]] = None
    maxTurns: Optional[int] = Field(default=None, gt=0)


class HermesTaskConfig(BaseModel):
    skills: Optional[List[str]] = None
    agent_hint: Optional[str] = None
    model_hint: Optional[str] = None


# ---------------------------------------------------------------------------
# DagNodeBase — common fields shared by all node types
# ---------------------------------------------------------------------------

_AGENT_ID_REGEX = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class DagNodeBase(BaseModel):
    """Common fields shared by all DAG node variants."""

    model_config = {"extra": "allow"}

    id: str
    phase: Optional[str] = None
    depends_on: Optional[List[str]] = None
    when: Optional[str] = None
    trigger_rule: Optional[TriggerRule] = None
    model: Optional[str] = None
    provider: Optional[str] = Field(default=None, min_length=1)
    context: Optional[Literal["fresh", "shared"]] = None
    output_format: Optional[Dict[str, Any]] = None
    allowed_tools: Optional[List[str]] = None
    denied_tools: Optional[List[str]] = None
    idle_timeout: Optional[float] = None
    retry: Optional[StepRetryConfig] = None
    hooks: Optional[WorkflowNodeHooks] = None
    mcp: Optional[str] = Field(default=None, min_length=1)
    skills: Optional[List[str]] = None
    agents: Optional[Dict[str, AgentDefinition]] = None
    effort: Optional[EffortLevel] = None
    thinking: Optional[Any] = None  # ThinkingConfig — parsed via validator
    maxBudgetUsd: Optional[float] = Field(default=None, gt=0)
    systemPrompt: Optional[str] = Field(default=None, min_length=1)
    fallbackModel: Optional[str] = Field(default=None, min_length=1)
    betas: Optional[List[str]] = None
    sandbox: Optional[SandboxSettings] = None
    hermes_task: Optional[HermesTaskConfig] = None

    @model_validator(mode="before")
    @classmethod
    def preprocess_thinking(cls, values: Any) -> Any:
        if isinstance(values, dict) and "thinking" in values:
            values["thinking"] = _parse_thinking_config(values["thinking"])
        return values


# ---------------------------------------------------------------------------
# Concrete node type models
# ---------------------------------------------------------------------------

class CommandNode(DagNodeBase):
    command: str


class PromptNode(DagNodeBase):
    prompt: str


class BashNode(DagNodeBase):
    bash: str
    timeout: Optional[float] = None


class ScriptNode(DagNodeBase):
    script: str = Field(..., min_length=1)
    runtime: Literal["bun", "uv"]
    deps: Optional[List[str]] = None
    timeout: Optional[float] = None


class LoopNode(DagNodeBase):
    loop: LoopNodeConfig


class ApprovalConfig(BaseModel):
    message: str = Field(..., min_length=1)
    capture_response: Optional[bool] = None
    on_reject: Optional["ApprovalOnReject"] = None


class ApprovalOnReject(BaseModel):
    prompt: str = Field(..., min_length=1)
    max_attempts: Optional[int] = Field(default=None, ge=1, le=10)


ApprovalConfig.model_rebuild()


class ApprovalNode(DagNodeBase):
    approval: ApprovalConfig


class CancelNode(DagNodeBase):
    cancel: str = Field(..., min_length=1)


class SubgraphReference(BaseModel):
    ref: str = Field(..., min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    inputs: Optional[Dict[str, Any]] = None
    when: Optional[str] = None
    timeout: Optional[int] = Field(default=None, gt=0)
    max_retries: Optional[int] = Field(default=None, ge=0)


class SubgraphNode(DagNodeBase):
    subgraph: SubgraphReference


# Union type matching TS DagNode
DagNode = Union[
    CommandNode,
    PromptNode,
    BashNode,
    LoopNode,
    ApprovalNode,
    CancelNode,
    ScriptNode,
    SubgraphNode,
]

# ---------------------------------------------------------------------------
# AI fields that are meaningless on non-AI nodes (mirrors TS constants)
# ---------------------------------------------------------------------------

BASH_NODE_AI_FIELDS: tuple[str, ...] = (
    "provider",
    "model",
    "context",
    "output_format",
    "allowed_tools",
    "denied_tools",
    "hooks",
    "mcp",
    "skills",
    "agents",
    "effort",
    "thinking",
    "maxBudgetUsd",
    "systemPrompt",
    "fallbackModel",
    "betas",
    "sandbox",
)

SCRIPT_NODE_AI_FIELDS = BASH_NODE_AI_FIELDS

LOOP_NODE_AI_FIELDS: tuple[str, ...] = tuple(
    f for f in BASH_NODE_AI_FIELDS if f not in ("model", "provider")
)

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_COMMAND_NAME_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-/]*$")


def _is_valid_command_name(name: str) -> bool:
    """Mirrors isValidCommandName from TS validation/command-validation.ts."""
    return bool(_COMMAND_NAME_REGEX.match(name)) and ".." not in name


def validate_dag_node(raw: Any, index: int) -> tuple[DagNode | None, list[str]]:
    """
    Validate and parse a single raw dict as a DagNode.

    Returns (node, []) on success or (None, [error, ...]) on failure.
    Enforces mutual exclusivity and other cross-field rules matching TS superRefine.
    """
    errors: list[str] = []

    if not isinstance(raw, dict):
        return None, [f"Node #{index + 1}: expected a mapping, got {type(raw).__name__}"]

    raw_id = str(raw.get("id", "")).strip()
    label = f"Node '{raw_id}'" if raw_id else f"Node #{index + 1}"

    if not raw_id:
        errors.append(f"{label}: missing required field 'id'")
        return None, errors

    has_command = isinstance(raw.get("command"), str) and raw["command"].strip()
    has_prompt = isinstance(raw.get("prompt"), str) and raw["prompt"].strip()
    has_bash = isinstance(raw.get("bash"), str) and raw["bash"].strip()
    has_loop = raw.get("loop") is not None
    has_approval = raw.get("approval") is not None
    has_cancel = isinstance(raw.get("cancel"), str) and raw["cancel"].strip()
    has_script = isinstance(raw.get("script"), str) and raw["script"].strip()
    has_subgraph = raw.get("subgraph") is not None

    mode_count = sum([
        bool(has_command), bool(has_prompt), bool(has_bash), bool(has_loop),
        bool(has_approval), bool(has_cancel), bool(has_script), bool(has_subgraph),
    ])

    if mode_count > 1:
        errors.append(
            f"{label}: 'command', 'prompt', 'bash', 'loop', 'approval', 'cancel', "
            "'script', and 'subgraph' are mutually exclusive"
        )
        return None, errors

    if mode_count == 0:
        if isinstance(raw.get("bash"), str):
            errors.append(f"{label}: bash script cannot be empty")
        elif isinstance(raw.get("prompt"), str):
            errors.append(f"{label}: prompt cannot be empty")
        elif isinstance(raw.get("script"), str):
            errors.append(f"{label}: script cannot be empty")
        else:
            errors.append(
                f"{label}: must have either 'command', 'prompt', 'bash', 'loop', "
                "'approval', 'cancel', 'script', or 'subgraph'"
            )
        return None, errors

    # Command name validation
    if has_command:
        cmd = raw["command"].strip()
        if not _is_valid_command_name(cmd):
            errors.append(f"{label}: invalid command name \"{cmd}\"")
            return None, errors

    # Bash/Script timeout validation
    if has_bash or has_script:
        timeout = raw.get("timeout")
        if timeout is not None:
            if not isinstance(timeout, (int, float)) or timeout <= 0 or not math.isfinite(timeout):
                errors.append(f"{label}: 'timeout' must be a positive number (ms)")
                return None, errors

    # Script requires runtime
    if has_script and raw.get("runtime") is None:
        errors.append(f"{label}: 'runtime' is required for script nodes ('bun' or 'uv')")
        return None, errors

    # Loop: retry not supported
    if has_loop and raw.get("retry") is not None:
        errors.append(
            f"{label}: 'retry' is not supported on loop nodes "
            "(loop manages its own iteration)"
        )
        return None, errors

    # idle_timeout must be finite positive
    idle_timeout = raw.get("idle_timeout")
    if idle_timeout is not None:
        if not isinstance(idle_timeout, (int, float)) or idle_timeout <= 0 or not math.isfinite(idle_timeout):
            errors.append(f"{label}: 'idle_timeout' must be a finite positive number (ms)")
            return None, errors

    # Try to construct the concrete model
    try:
        if has_command:
            return CommandNode.model_validate(raw), []
        if has_prompt:
            return PromptNode.model_validate(raw), []
        if has_bash:
            return BashNode.model_validate(raw), []
        if has_script:
            return ScriptNode.model_validate(raw), []
        if has_loop:
            return LoopNode.model_validate(raw), []
        if has_approval:
            # Coerce string shorthand: approval: "message" → approval: {message: "..."}
            coerced = dict(raw)
            if isinstance(coerced.get("approval"), str):
                coerced["approval"] = {"message": coerced["approval"]}
            return ApprovalNode.model_validate(coerced), []
        if has_cancel:
            return CancelNode.model_validate(raw), []
        if has_subgraph:
            return SubgraphNode.model_validate(raw), []
    except Exception as exc:
        errors.append(f"{label}: {exc}")
        return None, errors

    errors.append(f"{label}: unrecognized node mode (internal error)")
    return None, errors


# ---------------------------------------------------------------------------
# Type guards (mirrors TS type guards)
# ---------------------------------------------------------------------------

def is_bash_node(node: Any) -> bool:
    return isinstance(node, BashNode)

def is_loop_node(node: Any) -> bool:
    return isinstance(node, LoopNode)

def is_approval_node(node: Any) -> bool:
    return isinstance(node, ApprovalNode)

def is_cancel_node(node: Any) -> bool:
    return isinstance(node, CancelNode)

def is_script_node(node: Any) -> bool:
    return isinstance(node, ScriptNode)

def is_subgraph_node(node: Any) -> bool:
    return isinstance(node, SubgraphNode)

def is_trigger_rule(value: Any) -> bool:
    return isinstance(value, str) and value in TRIGGER_RULES
