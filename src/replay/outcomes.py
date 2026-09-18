"""
Closed outcome taxonomy for replay results.

The brief calls out that conflating "no such member" with "the replay
crashed" is the most common design mistake in this space. This module makes
that conflation structurally impossible: a caller has to look at `kind` to
get outputs at all, and `kind` can only be one of five values.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class OutcomeKind(str, Enum):
    # The artifact's checkpoint was reached and all declared outputs were
    # extracted. This is the only kind where `outputs` is guaranteed complete.
    SUCCESS = "success"

    # The app itself returned a legitimate, expected business result that
    # isn't "success" -- e.g. "no such member", "insufficient funds",
    # "permission denied". This is NOT a bug and NOT a crash; the caller
    # asked a real question and got a real answer. `business_code` is a
    # short machine-readable tag; `message` is human-readable detail.
    BUSINESS_OUTCOME = "business_outcome"

    # A transient/known condition was detected and handled automatically
    # (e.g. dismissed a cookie banner, retried a slow load) and the run then
    # completed successfully. Surfaced separately from plain SUCCESS so
    # flakiness/drift can be tracked over many replays even when the net
    # result was fine.
    RECOVERABLE_RETRIED = "recoverable_retried"

    # The engine could not safely continue (stuck, risky step needs
    # sign-off, or ran out of automatic recovery options) and handed off to
    # a human operator. `escalation_id` links to the intervention request.
    ESCALATED = "escalated"

    # Anything else: an unexpected error, an assertion that never became
    # true within timeout, an element that could not be resolved by any
    # locator candidate, a checkpoint that was never reached. Always
    # carries enough detail to debug.
    HARD_FAILURE = "hard_failure"


class ReplayResult(BaseModel):
    kind: OutcomeKind
    message: str
    outputs: dict[str, Any] = Field(default_factory=dict)

    # Populated only for BUSINESS_OUTCOME
    business_code: Optional[str] = None

    # Populated only for ESCALATED
    escalation_id: Optional[str] = None

    # Populated for HARD_FAILURE (and useful context on BUSINESS_OUTCOME too)
    failed_step_id: Optional[str] = None
    expected: Optional[str] = None
    observed: Optional[str] = None

    # Debug trail
    evidence_dir: Optional[str] = None
    steps_completed: int = 0
    steps_total: int = 0

    def is_success(self) -> bool:
        return self.kind in (OutcomeKind.SUCCESS, OutcomeKind.RECOVERABLE_RETRIED)
