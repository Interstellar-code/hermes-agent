"""
Pydantic model for step retry configuration.
Mirrors TS schemas/retry.ts exactly.
"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


class StepRetryConfig(BaseModel):
    """Per-node retry policy."""

    max_attempts: int = Field(
        ...,
        ge=1,
        le=5,
        description="Maximum retry attempts (not including the initial attempt). 1–5.",
    )
    delay_ms: Optional[float] = Field(
        default=None,
        ge=1000,
        le=60000,
        description="Initial delay in ms, doubled on each attempt. 1000–60000.",
    )
    on_error: Optional[Literal["transient", "all"]] = Field(
        default=None,
        description="Which error types trigger a retry. Default: 'transient'.",
    )
