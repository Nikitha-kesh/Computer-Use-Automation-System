"""
Deterministic replay engine.

Given a saved CapabilityArtifact and a dict of input params, executes the
recorded steps in order using the same action primitives the discovery
loop used (src/agent/actions.py), with no model call anywhere in this
module. This is the path a production AI agent would invoke.

Error/outcome handling implements the three-way split the brief asks for:

  1. Business outcomes: detected via a caller-supplied `detect_business_outcome`
     hook that inspects the live page for known "this is a legitimate
     app-level answer" signals (e.g. a visible validation/not-found banner).
     Checked after every step so it can fire even on an action that
     otherwise "succeeded" (the app accepted the input but responded with
     an error message rather than raising a Playwright-level failure).
  2. Recoverable conditions: known interstitials (e.g. a cookie banner) are
     dismissed automatically via `interstitial_rules` before a step is
     retried; retryable step failures (timeout, transient load) are retried
     per the step's own RetryPolicy before anything else happens.
  3. Hard failures / escalation: if a step still can't proceed after
     retries and no business outcome explains it, the engine raises an
     intervention request and hands control of the *same* live session to
     a human. If the human resolves it, the engine retries the step once
     more; if not, replay stops and reports ESCALATED. Anything else
     unexpected (checkpoint never reached, locator never resolves at all)
     is a HARD_FAILURE with step/expected/observed detail for debugging.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

from playwright.sync_api import Page

from src.agent.actions import assert_checkpoint, execute_action
from src.artifact.schema import ActionType, CapabilityArtifact, Step
from src.escalation.manager import EscalationManager
from src.evidence.logger import EvidenceLogger
from src.replay.outcomes import OutcomeKind, ReplayResult
from src.safety.policy import AllowlistConfig, RiskDecision, RiskPolicy

_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")

BusinessOutcomeDetector = Callable[[Page], Optional[tuple[str, str]]]  # -> (code, message) or None
InterstitialRule = Callable[[Page], bool]  # returns True if it found & dismissed something


def resolve_template(template: Optional[str], values: dict[str, Any]) -> Optional[str]:
    if template is None:
        return None

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"template references unknown value '{key}'")
        return str(values[key])

    return _TEMPLATE_RE.sub(sub, template)


def replay(
    artifact: CapabilityArtifact,
    input_args: dict[str, Any],
    page: Page,
    allowlist: AllowlistConfig,
    evidence: EvidenceLogger,
    risk_policy: Optional[RiskPolicy] = None,
    detect_business_outcome: Optional[BusinessOutcomeDetector] = None,
    interstitial_rules: Optional[list[InterstitialRule]] = None,
    interactive_escalation: bool = True,
) -> ReplayResult:
    risk_policy = risk_policy or RiskPolicy()
    interstitial_rules = interstitial_rules or []
    escalation = EscalationManager(evidence, page, interactive=interactive_escalation)

    errors = artifact.validate_call_args(input_args)
    if errors:
        evidence.log_event("replay_input_validation_failed", errors=errors)
        return ReplayResult(kind=OutcomeKind.HARD_FAILURE, message="input validation failed: " + "; ".join(errors))

    outputs: dict[str, Any] = {}
    values: dict[str, Any] = dict(input_args)  # grows with extracted outputs, for templates that reference them
    escalation_id: Optional[str] = None

    evidence.log_event("replay_started", artifact_id=artifact.artifact_id, version=artifact.version, args=list(input_args.keys()))

    def _check_business_outcome() -> Optional[ReplayResult]:
        if detect_business_outcome is None:
            return None
        hit = detect_business_outcome(page)
        if hit is None:
            return None
        code, message = hit
        evidence.log_event("business_outcome_detected", code=code, message=message)
        evidence.save_screenshot(page, "business_outcome")
        return ReplayResult(
            kind=OutcomeKind.BUSINESS_OUTCOME, message=message, business_code=code,
            outputs=outputs, evidence_dir=evidence.dir,
            steps_completed=0, steps_total=len(artifact.steps),
        )

    def _try_dismiss_interstitials() -> bool:
        dismissed_any = False
        for rule in interstitial_rules:
            try:
                if rule(page):
                    dismissed_any = True
            except Exception:
                continue
        return dismissed_any

    steps_completed = 0
    had_recoverable = False

    for step in artifact.steps:
        # allowlist checks
        if step.action == ActionType.NAVIGATE:
            url = resolve_template(step.value_template, values) or ""
            ok, why = allowlist.check_url(url)
            if not ok:
                evidence.log_event("allowlist_violation", step_id=step.step_id, url=url, reason=why)
                return ReplayResult(kind=OutcomeKind.HARD_FAILURE, message=f"allowlist violation: {why}", failed_step_id=step.step_id, steps_completed=steps_completed, steps_total=len(artifact.steps))
        ok, why = allowlist.check_action(step.action)
        if not ok:
            evidence.log_event("allowlist_violation", step_id=step.step_id, action=step.action.value, reason=why)
            return ReplayResult(kind=OutcomeKind.HARD_FAILURE, message=f"allowlist violation: {why}", failed_step_id=step.step_id, steps_completed=steps_completed, steps_total=len(artifact.steps))

        # risk policy
        decision = risk_policy.decide(step.risk, artifact_approved=(artifact.review_status.value == "approved"))
        if decision == RiskDecision.BLOCK:
            evidence.log_event("risk_blocked", step_id=step.step_id, risk=step.risk.value)
            return ReplayResult(kind=OutcomeKind.HARD_FAILURE, message=f"step {step.step_id} blocked by risk policy ({step.risk.value})", failed_step_id=step.step_id, steps_completed=steps_completed, steps_total=len(artifact.steps))
        if decision == RiskDecision.REQUIRE_CONFIRMATION:
            req = escalation.raise_intervention(
                capability_name=artifact.name, context_ref=artifact.artifact_id, step_id=step.step_id,
                reason=f"step is {step.risk.value}: {step.description}",
            )
            escalation_id = req.request_id
            approved = escalation.handoff_to_human(req)
            if not approved:
                return ReplayResult(
                    kind=OutcomeKind.ESCALATED, message="human operator did not approve the irreversible step",
                    escalation_id=escalation_id, failed_step_id=step.step_id,
                    outputs=outputs, steps_completed=steps_completed, steps_total=len(artifact.steps),
                )

        # resolve template value
        try:
            value = resolve_template(step.value_template, values)
        except KeyError as e:
            return ReplayResult(kind=OutcomeKind.HARD_FAILURE, message=f"template resolution failed: {e}", failed_step_id=step.step_id, steps_completed=steps_completed, steps_total=len(artifact.steps))

        # execute with retry
        attempts = 0
        result = None
        while attempts < step.retry.max_attempts:
            attempts += 1
            result = execute_action(page, step.action, locator=step.locator, value=value, timeout_ms=step.timeout_ms)
            if result.ok:
                break
            bo = _check_business_outcome()
            if bo:
                return bo
            if _try_dismiss_interstitials():
                had_recoverable = True
                time.sleep(0.3)
                continue  # retry immediately after clearing an interstitial
            if attempts < step.retry.max_attempts:
                time.sleep(step.retry.backoff_ms / 1000)

        evidence.log_event(
            "replay_step", step_id=step.step_id, action=step.action.value,
            ok=result.ok if result else False, error=result.error if result else None, attempts=attempts,
        )

        if result is None or not result.ok:
            bo = _check_business_outcome()
            if bo:
                return bo
            evidence.save_screenshot(page, f"failure_{step.step_id}")
            # last resort: escalate
            req = escalation.raise_intervention(
                capability_name=artifact.name, context_ref=artifact.artifact_id, step_id=step.step_id,
                reason=f"step failed after {attempts} attempt(s): {result.error if result else 'unknown'}",
            )
            escalation_id = req.request_id
            approved = escalation.handoff_to_human(req)
            if not approved:
                return ReplayResult(
                    kind=OutcomeKind.ESCALATED, message="human operator did not resolve the failed step",
                    escalation_id=escalation_id, failed_step_id=step.step_id,
                    expected=step.description, observed=(result.error if result else None),
                    outputs=outputs, steps_completed=steps_completed, steps_total=len(artifact.steps),
                )
            # human resolved it manually -- re-check the page rather than blindly retrying the same action
            bo = _check_business_outcome()
            if bo:
                return bo
            steps_completed += 1
            continue

        if step.action == ActionType.EXTRACT and step.output_binding:
            outputs[step.output_binding] = result.extracted_value
            values[step.output_binding] = result.extracted_value

        steps_completed += 1

    # final checkpoint
    bo = _check_business_outcome()
    if bo:
        return bo

    cp = artifact.checkpoint
    try:
        expected = resolve_template(cp.expected_value, values)
    except KeyError as e:
        return ReplayResult(kind=OutcomeKind.HARD_FAILURE, message=f"checkpoint template resolution failed: {e}", steps_completed=steps_completed, steps_total=len(artifact.steps))

    cp_result = assert_checkpoint(page, cp.locator, cp.condition, expected, timeout_ms=8000)
    if not cp_result.ok:
        evidence.save_screenshot(page, "checkpoint_failed")
        return ReplayResult(
            kind=OutcomeKind.HARD_FAILURE, message=f"checkpoint not satisfied: {cp_result.error}",
            failed_step_id="checkpoint", expected=expected, observed=cp_result.error,
            outputs=outputs, steps_completed=steps_completed, steps_total=len(artifact.steps),
        )

    evidence.log_event("replay_succeeded", outputs=outputs, had_recoverable=had_recoverable)
    return ReplayResult(
        kind=OutcomeKind.RECOVERABLE_RETRIED if had_recoverable else OutcomeKind.SUCCESS,
        message="checkpoint reached",
        outputs=outputs,
        escalation_id=escalation_id,
        steps_completed=steps_completed,
        steps_total=len(artifact.steps),
        evidence_dir=evidence.dir,
    )
