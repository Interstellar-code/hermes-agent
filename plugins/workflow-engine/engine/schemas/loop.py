"""
Pydantic model for loop node configuration.
Mirrors TS schemas/loop.ts exactly.
"""
from __future__ import annotations

from typing import Any, List, Optional
from pydantic import BaseModel, Field, model_validator


class LoopNodeConfig(BaseModel):
    """Configuration for a loop DAG node."""

    over: Optional[List[Any]] = Field(
        default=None,
        description="List of items to iterate over. When set, one iteration per item; $LOOP_ITEM is substituted.",
    )
    prompt: str = Field(
        ...,
        min_length=1,
        description="Inline prompt text executed each iteration.",
    )
    until: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Completion signal string detected in AI output (e.g., 'COMPLETE'). Required for AI loops; omit for 'over' list loops.",
    )
    max_iterations: int = Field(
        default=1,
        gt=0,
        description="Maximum iterations allowed; exceeding this fails the node.",
    )
    fresh_context: bool = Field(
        default=False,
        description="Whether to start fresh session each iteration.",
    )
    until_bash: Optional[str] = Field(
        default=None,
        description="Optional bash script run after each iteration; exit 0 = complete.",
    )
    interactive: Optional[bool] = Field(
        default=None,
        description="When true, pause between iterations for user input.",
    )
    gate_message: Optional[str] = Field(
        default=None,
        description="Message shown to user when paused (required when interactive is true).",
    )

    @model_validator(mode="after")
    def check_interactive_gate_message(self) -> "LoopNodeConfig":
        if self.interactive is True and not self.gate_message:
            raise ValueError(
                "interactive loop requires 'loop.gate_message' (non-empty string)"
            )
        return self
