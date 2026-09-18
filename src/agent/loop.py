"""
The discovery loop: an LLM-driven observe -> decide -> act cycle against a
live Playwright page. Produces a `Transcript` (list of `TranscriptEntry`)
that `artifact/recorder.py` turns into a reusable CapabilityArtifact.

This is the *only* place in the system where the LLM makes decisions.
Everything downstream of a successful run (the artifact, its replay) never
calls the model again -- that boundary is the whole point of the system.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from playwright.sync_api import Page

from src.agent.actions import ActionExecutionResult, execute_action
from src.agent.llm_client import DiscoveryAgentClient
from src.agent.perception import PerceivedElement, perceive, render_for_llm
from src.artifact.schema import ActionType, RiskLevel
from src.escalation.manager import EscalationManager
from src.evidence.logger import EvidenceLogger
from src.replay.locator_resolver import build_locator_from_perceived
from src.safety.policy import AllowlistConfig, RiskDecision, RiskPolicy, classify_risk

MAX_STUCK_RETRIES = 3


@dataclass
class TranscriptEntry:
    step_index: int
    tool_name: str
    action: ActionType
    element: Optional[PerceivedElement]
    value: Optional[str]
    param_name: Optional[str]
    output_name: Optional[str]
    extracted_value: Any
    risk: RiskLevel
    result_ok: bool
    error: Optional[str]


@dataclass
class DiscoveryOutcome:
    success: bool
    summary: str
    transcript: list[TranscriptEntry] = field(default_factory=list)
    checkpoint_element: Optional[PerceivedElement] = None
    checkpoint_expected_text: Optional[str] = None
    escalated: bool = False


def run_discovery(
    page: Page,
    goal: str,
    target_url: str,
    app_id: str,
    declared_params: list[dict],
    allowlist: AllowlistConfig,
    evidence: EvidenceLogger,
    max_steps: int = 25,
    interactive_escalation: bool = True,
    model: Optional[str] = None,
    llm_client: Optional[object] = None,
) -> DiscoveryOutcome:
    """
    `llm_client`, if given, must implement the same interface as
    DiscoveryAgentClient (start / observe_and_decide / report_tool_result).
    This is the seam that lets the loop be exercised end-to-end with a
    scripted fake in tests, without needing live model access -- see
    tests/test_discovery_pipeline.py.
    """
    llm = llm_client or (DiscoveryAgentClient(model=model) if model else DiscoveryAgentClient())
    llm.start(goal=goal, target_url=target_url, declared_params=declared_params)
    escalation = EscalationManager(evidence, page, interactive=interactive_escalation)
    risk_policy = RiskPolicy()

    transcript: list[TranscriptEntry] = []
    last_result_text: Optional[str] = None
    consecutive_failures = 0

    evidence.log_event("discovery_started", goal=goal, target_url=target_url, app_id=app_id)

    for step_index in range(max_steps):
        elements = perceive(page)
        elements_desc = render_for_llm(elements)
        turn = llm.observe_and_decide(elements_desc, last_action_result=last_result_text)
        evidence.log_event("llm_turn", step=step_index, text=turn.raw_text, stop_reason=turn.stop_reason)

        if turn.tool_call is None:
            last_result_text = "No tool call received; you must call exactly one tool."
            continue

        tc = turn.tool_call
        evidence.log_event("tool_call", step=step_index, name=tc.name, input=tc.input)

        # ---- finish -----------------------------------------------------
        if tc.name == "finish":
            success = bool(tc.input.get("success"))
            summary = tc.input.get("summary", "")
            checkpoint_el = None
            if "checkpoint_element_index" in tc.input:
                idx = tc.input["checkpoint_element_index"]
                checkpoint_el = next((e for e in elements if e.index == idx), None)
            evidence.log_event("discovery_finished", success=success, summary=summary)
            evidence.save_screenshot(page, "final_state")
            return DiscoveryOutcome(
                success=success,
                summary=summary,
                transcript=transcript,
                checkpoint_element=checkpoint_el,
                checkpoint_expected_text=tc.input.get("checkpoint_expected_text"),
            )

        # ---- request_human_help ------------------------------------------
        if tc.name == "request_human_help":
            reason = tc.input.get("reason", "model requested help")
            req = escalation.raise_intervention(capability_name=app_id, context_ref=goal, reason=reason)
            approved = escalation.handoff_to_human(req)
            last_result_text = (
                "A human operator intervened and approved continuing. Re-observe the page state "
                "(it may have changed) and proceed."
                if approved else
                "A human operator was consulted and rejected/could not resolve this. Consider calling finish(success=false)."
            )
            if not approved:
                consecutive_failures += 1
            continue

        # ---- resolve element / action type -------------------------------
        action_map = {
            "navigate": ActionType.NAVIGATE,
            "click": ActionType.CLICK,
            "fill": ActionType.FILL,
            "select": ActionType.SELECT,
            "check": ActionType.CHECK,
            "press_key": ActionType.PRESS_KEY,
            "extract": ActionType.EXTRACT,
        }

        if tc.name == "wait":
            secs = min(float(tc.input.get("seconds", 1)), 10.0)
            time.sleep(secs)
            llm.report_tool_result(tc.id, f"waited {secs}s")
            last_result_text = None
            continue

        if tc.name not in action_map:
            llm.report_tool_result(tc.id, f"unknown tool {tc.name}", is_error=True)
            last_result_text = f"Unknown tool {tc.name}."
            continue

        action = action_map[tc.name]
        element = None
        element_name = ""
        if "element_index" in tc.input:
            element = next((e for e in elements if e.index == tc.input["element_index"]), None)
            if element is None:
                llm.report_tool_result(tc.id, "element_index not found in current element list", is_error=True)
                last_result_text = "That element_index no longer exists; re-check the current element list."
                consecutive_failures += 1
                continue
            element_name = element.name

        # ---- safety: allowlist -------------------------------------------
        if action == ActionType.NAVIGATE:
            url = tc.input.get("url", "")
            ok, why = allowlist.check_url(url)
            if not ok:
                evidence.log_event("allowlist_violation", step=step_index, url=url, reason=why)
                llm.report_tool_result(tc.id, f"BLOCKED by allowlist: {why}", is_error=True)
                last_result_text = f"Navigation blocked by policy: {why}"
                continue

        ok, why = allowlist.check_action(action)
        if not ok:
            evidence.log_event("allowlist_violation", step=step_index, action=action.value, reason=why)
            llm.report_tool_result(tc.id, f"BLOCKED by allowlist: {why}", is_error=True)
            last_result_text = f"Action blocked by policy: {why}"
            continue

        # ---- safety: risk policy -------------------------------------------
        risk = classify_risk(action, element_name)
        decision = risk_policy.decide(risk, artifact_approved=False)
        if decision == RiskDecision.BLOCK:
            evidence.log_event("risk_blocked", step=step_index, action=action.value, element=element_name)
            llm.report_tool_result(tc.id, "BLOCKED: this action is classified irreversible and blocked by policy", is_error=True)
            last_result_text = "That action is blocked by policy."
            continue
        if decision == RiskDecision.REQUIRE_CONFIRMATION:
            req = escalation.raise_intervention(
                capability_name=app_id, context_ref=goal,
                reason=f"about to perform risky/irreversible action: {action.value} on '{element_name}'",
            )
            approved = escalation.handoff_to_human(req)
            if not approved:
                llm.report_tool_result(tc.id, "Human operator rejected this irreversible action.", is_error=True)
                last_result_text = "The irreversible action was rejected by a human operator. Do not retry it; consider finish(success=false)."
                continue
            # approved -> fall through and actually execute

        # ---- build locator + execute --------------------------------------
        value = tc.input.get("url") if action == ActionType.NAVIGATE else tc.input.get("value")
        locator = build_locator_from_perceived(element) if element else None
        result: ActionExecutionResult = execute_action(page, action, locator=locator, value=value, timeout_ms=5000)

        extracted = result.extracted_value
        transcript.append(TranscriptEntry(
            step_index=step_index,
            tool_name=tc.name,
            action=action,
            element=element,
            value=value,
            param_name=tc.input.get("param_name"),
            output_name=tc.input.get("output_name"),
            extracted_value=extracted,
            risk=risk,
            result_ok=result.ok,
            error=result.error,
        ))
        evidence.log_event(
            "action_executed", step=step_index, action=action.value, element=element_name,
            ok=result.ok, error=result.error, matched_strategy=str(result.matched_strategy),
        )

        if result.ok:
            consecutive_failures = 0
            if action == ActionType.EXTRACT:
                last_result_text = f"Extracted value for '{tc.input.get('output_name')}': {extracted!r}"
            else:
                last_result_text = "OK."
            llm.report_tool_result(tc.id, last_result_text)
        else:
            consecutive_failures += 1
            evidence.save_screenshot(page, f"failure_step_{step_index}")
            last_result_text = f"Action failed: {result.error}"
            llm.report_tool_result(tc.id, last_result_text, is_error=True)

        if consecutive_failures >= MAX_STUCK_RETRIES:
            req = escalation.raise_intervention(
                capability_name=app_id, context_ref=goal,
                reason=f"{consecutive_failures} consecutive action failures; agent appears stuck",
            )
            approved = escalation.handoff_to_human(req)
            if not approved:
                evidence.log_event("discovery_aborted", reason="stuck, human did not resolve")
                return DiscoveryOutcome(success=False, summary="Agent got stuck and human operator did not resolve it.", transcript=transcript, escalated=True)
            consecutive_failures = 0
            last_result_text = "A human operator intervened to unblock you. Re-observe and continue."

    evidence.log_event("discovery_max_steps_exceeded")
    return DiscoveryOutcome(success=False, summary=f"Exceeded max_steps ({max_steps}) without finishing.", transcript=transcript)
