"""
Capability artifact schema.

An Artifact is what a discovery run produces and what deterministic replay
consumes. It is the contract between:
  - a human reviewer (can this be trusted to run unattended?)
  - a calling AI agent (what do I pass in, what do I get back?)
  - the replay engine (how do I find each control, and how do I know I
    actually got where I meant to go?)

Design choices (see /REPORT.md section 2 for the full rationale):

- Locators are *ranked candidate lists*, not a single selector. Each
  candidate carries the strategy used and a confidence score assigned at
  record time. Replay tries them in order and takes the first that resolves
  to exactly one visible, enabled element. This is what lets replay survive
  minor DOM changes without re-recording, and is the seam that generalizes
  to legacy/no-clean-DOM surfaces (see schema note on LocatorStrategy).
- Steps reference input params by name in `value_template` rather than
  embedding literal values, so one artifact is reusable across invocations
  (and, per the multi-tenant design in REPORT.md, across tenants running
  variants of the same app).
- Every step is tagged with a RiskLevel so the replay engine and the safety
  layer can apply different handling (e.g. block/require-confirmation) to
  irreversible steps without the artifact author having to build that logic
  themselves each time.
- Outcomes are modeled as a closed taxonomy (schema in replay/outcomes.py)
  so "no such member" and "the replay crashed" can never be confused by a
  calling agent that only checks a boolean.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------
# Locators
# --------------------------------------------------------------------------

class LocatorStrategy(str, Enum):
    """
    Ordered (by the record-time confidence we generally assign) from most to
    least robust. Robustness reasoning:

    - ROLE_NAME: accessibility role + accessible name. Survives markup/CSS
      rewrites, works identically on a11y trees for native desktop apps too
      (see REPORT.md section 4 on surface abstraction). Preferred whenever
      the element exposes a sensible role/name.
    - TEST_ID: an explicit data-testid/automation-id. Most stable *when
      present*, but per the glossary, legacy enterprise apps essentially
      never have these -- so it's ranked below ROLE_NAME, not above it,
      despite being conceptually "designed for automation."
    - LABEL_TEXT: resolve via an associated <label> or nearby caption text.
      Common on forms; reasonably stable because labels are user-facing
      copy that changes rarely (compared to markup) but does still change
      with re-copy/localization.
    - CSS: structural CSS selector. Brittle under refactors and the most
      likely candidate to break on a differently-configured tenant of the
      same vendor product. Kept as a fallback, not a primary.
    - XPATH: same brittleness profile as CSS, sometimes the only way to
      reach into deeply nested legacy table markup or framesets.
    - TEXT: exact/contains visible text match. Fragile to copy changes but
      often the *only* signal available on a screenshot-only surface.
    """
    ROLE_NAME = "role_name"
    TEST_ID = "test_id"
    LABEL_TEXT = "label_text"
    CSS = "css"
    XPATH = "xpath"
    TEXT = "text"


class LocatorCandidate(BaseModel):
    strategy: LocatorStrategy
    value: str
    # For frameset/iframe-heavy legacy surfaces: ordered path of frame
    # names/selectors to descend into before applying `value`. Empty = main
    # frame. This is the seam that lets the same Step shape address a
    # frameset app without changing the action model.
    frame_path: list[str] = Field(default_factory=list)
    # 0-1, assigned at record time (e.g. ROLE_NAME with a unique accessible
    # name -> high confidence; a positional CSS nth-child -> low). Replay
    # tries candidates in list order, not by confidence, but confidence is
    # surfaced to reviewers and used by the "confidence & approval" scoring
    # if that stretch goal is enabled.
    confidence: float = 0.5


class ElementLocator(BaseModel):
    description: str  # human-readable, e.g. "First Name input"
    candidates: list[LocatorCandidate]

    @field_validator("candidates")
    @classmethod
    def _non_empty(cls, v: list[LocatorCandidate]) -> list[LocatorCandidate]:
        if not v:
            raise ValueError("ElementLocator requires at least one candidate")
        return v


# --------------------------------------------------------------------------
# Params / outputs
# --------------------------------------------------------------------------

class ParamType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    ENUM = "enum"


class InputParam(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str
    enum_values: Optional[list[str]] = None
    # Marks this param as sensitive (PII/secret-adjacent). The evidence
    # logger and artifact store redact any *logged* value for pii params;
    # the value itself still flows through the live run since the replay
    # needs it to fill the form -- redaction targets persistence, not use.
    pii: bool = False

    @field_validator("name")
    @classmethod
    def _valid_identifier(cls, v: str) -> str:
        if not re.match(r"^[a-z][a-z0-9_]*$", v):
            raise ValueError(f"param name {v!r} must be snake_case identifier")
        return v


class OutputField(BaseModel):
    name: str
    type: ParamType
    description: str
    source_step_id: str


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"          # native <select> or custom listbox-style control
    CHECK = "check"
    PRESS_KEY = "press_key"
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"        # read text/value from an element into an output
    ASSERT_CHECKPOINT = "assert_checkpoint"


class RiskLevel(str, Enum):
    SAFE = "safe"                        # read-only / trivially reversible (navigate, extract, fill-not-yet-submitted)
    RISKY_REVERSIBLE = "risky_reversible"     # mutates state but can be undone (e.g. save a draft)
    RISKY_IRREVERSIBLE = "risky_irreversible"  # submits a transaction, opens an account, sends money


class RetryPolicy(BaseModel):
    max_attempts: int = 2
    backoff_ms: int = 500
    # Conditions under which a failed attempt is retried at all (vs. treated
    # as a hard failure immediately). Kept deliberately small & explicit.
    retry_on: list[str] = Field(
        default_factory=lambda: ["timeout", "transient_load"]
    )


class Step(BaseModel):
    step_id: str
    description: str
    action: ActionType
    locator: Optional[ElementLocator] = None
    # Template string that may reference input params as {{param_name}}.
    # Resolved by the replay engine's ValueResolver before use. Kept as a
    # string (not raw literal) so the same artifact works for every call.
    value_template: Optional[str] = None
    output_binding: Optional[str] = None  # name of the OutputField this step populates (EXTRACT steps)
    risk: RiskLevel = RiskLevel.SAFE
    timeout_ms: int = 5000
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


# --------------------------------------------------------------------------
# Checkpoint
# --------------------------------------------------------------------------

class CheckpointCondition(str, Enum):
    VISIBLE = "visible"
    TEXT_EQUALS = "text_equals"
    TEXT_CONTAINS = "text_contains"
    URL_MATCHES = "url_matches"


class Checkpoint(BaseModel):
    description: str
    locator: Optional[ElementLocator] = None
    condition: CheckpointCondition
    expected_value: Optional[str] = None  # template, may reference {{param}} or {{output}}


# --------------------------------------------------------------------------
# Target / provenance / risk summary
# --------------------------------------------------------------------------

class TargetSpec(BaseModel):
    app_id: str                     # logical app identifier, e.g. "demoqa-practice-form"
    base_url: str
    vendor_product: Optional[str] = None   # for multi-tenant reuse: shared vendor product id
    tenant_id: Optional[str] = None        # None = tenant-agnostic base artifact


class Provenance(BaseModel):
    recorded_by_model: str
    discovery_run_id: str
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReviewStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class RiskSummary(BaseModel):
    highest_risk: RiskLevel
    irreversible_step_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Top-level artifact
# --------------------------------------------------------------------------

class CapabilityArtifact(BaseModel):
    schema_version: str = SCHEMA_VERSION
    artifact_id: str
    name: str
    version: str  # semver, bumped on re-record
    description: str
    target: TargetSpec
    input_params: list[InputParam]
    output_fields: list[OutputField]
    steps: list[Step]
    checkpoint: Checkpoint
    risk_summary: RiskSummary
    provenance: Provenance
    review_status: ReviewStatus = ReviewStatus.DRAFT

    def input_param_names(self) -> set[str]:
        return {p.name for p in self.input_params}

    def validate_call_args(self, args: dict[str, Any]) -> list[str]:
        """Return a list of validation error strings (empty = valid)."""
        errors = []
        declared = {p.name: p for p in self.input_params}
        for name, p in declared.items():
            if p.required and name not in args:
                errors.append(f"missing required param: {name}")
        for name in args:
            if name not in declared:
                errors.append(f"unexpected param: {name}")
        return errors
