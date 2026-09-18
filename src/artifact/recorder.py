"""
Turns a successful discovery Transcript into a CapabilityArtifact.

Only successful, executed actions become Steps -- the artifact is a clean
recipe for the path that worked, not a log of everything the model tried.
Retries/failed attempts stay in the evidence trail (run.jsonl) for
debugging, but are deliberately excluded from the artifact itself so that
replay doesn't replay the model's mistakes.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from src.agent.loop import DiscoveryOutcome, TranscriptEntry
from src.artifact.schema import (
    ActionType,
    Checkpoint,
    CheckpointCondition,
    CapabilityArtifact,
    ElementLocator,
    InputParam,
    OutputField,
    ParamType,
    Provenance,
    RiskLevel,
    RiskSummary,
    Step,
    TargetSpec,
)
from src.replay.locator_resolver import build_locator_from_perceived

_RISK_ORDER = {RiskLevel.SAFE: 0, RiskLevel.RISKY_REVERSIBLE: 1, RiskLevel.RISKY_IRREVERSIBLE: 2}

_PII_NAME_HINTS = re.compile(r"(ssn|social.?security|dob|birth|phone|mobile|email|address|account.?number)", re.IGNORECASE)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "capability"


def build_artifact(
    outcome: DiscoveryOutcome,
    goal: str,
    app_id: str,
    base_url: str,
    target_url: str,
    discovery_run_id: str,
    model_name: str,
    vendor_product: str | None = None,
) -> CapabilityArtifact:
    if not outcome.success:
        raise ValueError("cannot build an artifact from a failed discovery run")

    steps: list[Step] = []
    params_seen: dict[str, InputParam] = {}
    outputs_seen: dict[str, OutputField] = {}

    # Step 0 is always an explicit navigate to the entry point.
    steps.append(Step(
        step_id="step_0_navigate",
        description=f"Open {target_url}",
        action=ActionType.NAVIGATE,
        value_template=target_url,
        risk=RiskLevel.SAFE,
    ))

    for i, entry in enumerate(outcome.transcript):
        if not entry.result_ok:
            continue  # only the successful path is baked into the artifact
        if entry.action == ActionType.NAVIGATE:
            continue  # already covered by step_0, or a mid-flow nav we treat the same way below

        step_id = f"step_{i + 1}_{entry.action.value}"
        locator = build_locator_from_perceived(entry.element) if entry.element else None

        value_template = entry.value
        if entry.param_name:
            value_template = "{{" + entry.param_name + "}}"
            if entry.param_name not in params_seen:
                params_seen[entry.param_name] = InputParam(
                    name=entry.param_name,
                    type=ParamType.STRING,
                    required=True,
                    description=f"Value for '{entry.element.name if entry.element else entry.param_name}'",
                    pii=bool(_PII_NAME_HINTS.search(entry.param_name)) or bool(
                        entry.element and _PII_NAME_HINTS.search(entry.element.name or "")
                    ),
                )

        output_binding = None
        if entry.action == ActionType.EXTRACT and entry.output_name:
            output_binding = entry.output_name
            if entry.output_name not in outputs_seen:
                outputs_seen[entry.output_name] = OutputField(
                    name=entry.output_name,
                    type=ParamType.STRING,
                    description=f"Extracted from '{entry.element.name if entry.element else entry.output_name}'",
                    source_step_id=step_id,
                )

        steps.append(Step(
            step_id=step_id,
            description=f"{entry.action.value} on '{entry.element.name if entry.element else ''}'"
                        + (f" = {value_template}" if value_template else ""),
            action=entry.action,
            locator=locator,
            value_template=value_template,
            output_binding=output_binding,
            risk=entry.risk,
        ))

    # Checkpoint
    if outcome.checkpoint_element is not None:
        checkpoint = Checkpoint(
            description=f"'{outcome.checkpoint_element.name}' confirms goal completion",
            locator=build_locator_from_perceived(outcome.checkpoint_element),
            condition=CheckpointCondition.TEXT_CONTAINS if outcome.checkpoint_expected_text else CheckpointCondition.VISIBLE,
            expected_value=outcome.checkpoint_expected_text,
        )
    else:
        checkpoint = Checkpoint(
            description="Fallback checkpoint: URL reached at end of discovery run",
            condition=CheckpointCondition.URL_MATCHES,
            expected_value=target_url,
        )

    irreversible_ids = [s.step_id for s in steps if s.risk == RiskLevel.RISKY_IRREVERSIBLE]
    highest = max((s.risk for s in steps), key=lambda r: _RISK_ORDER[r], default=RiskLevel.SAFE)

    name = f"{app_id}: {goal}"
    artifact_id = f"{_slugify(app_id)}__{_slugify(goal)}"

    return CapabilityArtifact(
        artifact_id=artifact_id,
        name=name,
        version="1.0.0",
        description=goal,
        target=TargetSpec(app_id=app_id, base_url=base_url, vendor_product=vendor_product),
        input_params=list(params_seen.values()),
        output_fields=list(outputs_seen.values()),
        steps=steps,
        checkpoint=checkpoint,
        risk_summary=RiskSummary(highest_risk=highest, irreversible_step_ids=irreversible_ids),
        provenance=Provenance(recorded_by_model=model_name, discovery_run_id=discovery_run_id),
    )
