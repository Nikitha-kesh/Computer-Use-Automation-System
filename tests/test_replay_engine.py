"""
These tests hand-build a CapabilityArtifact (bypassing the LLM entirely)
and run it through the real deterministic replay engine against a real
Playwright-controlled browser talking to the local mock bank app. This is
what validates that the artifact schema, locator resolver, action
primitives, safety layer, and outcome taxonomy actually work end-to-end,
independent of model availability/network access.

The genuine LLM-driven discovery run (required by the brief) is a separate,
documented manual step -- see README.md -- since it needs a live API key.
"""

from __future__ import annotations

from src.artifact.schema import (
    ActionType,
    Checkpoint,
    CheckpointCondition,
    CapabilityArtifact,
    ElementLocator,
    InputParam,
    LocatorCandidate,
    LocatorStrategy,
    OutputField,
    ParamType,
    Provenance,
    RiskLevel,
    RiskSummary,
    Step,
    TargetSpec,
)
from src.evidence.logger import EvidenceLogger
from src.replay.engine import replay
from src.safety.policy import AllowlistConfig


def _loc(desc: str, dom_id: str) -> ElementLocator:
    return ElementLocator(
        description=desc,
        candidates=[LocatorCandidate(strategy=LocatorStrategy.CSS, value=f"#{dom_id}", confidence=0.9)],
    )


def _role_loc(desc: str, role: str, name: str) -> ElementLocator:
    return ElementLocator(
        description=desc,
        candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value=f"{role}::{name}", confidence=0.9)],
    )


def build_open_account_artifact() -> CapabilityArtifact:
    steps = [
        Step(step_id="s0_nav", description="open lookup page", action=ActionType.NAVIGATE, value_template="{{base_url}}/"),
        Step(step_id="s1_cookie", description="dismiss cookie banner if present", action=ActionType.CLICK,
             locator=_loc("Accept cookies", "cookie-accept"), risk=RiskLevel.SAFE,
             retry=__import__("src.artifact.schema", fromlist=["RetryPolicy"]).RetryPolicy(max_attempts=1)),
        Step(step_id="s2_fill_member", description="fill member id", action=ActionType.FILL,
             locator=_loc("Member ID input", "member_id"), value_template="{{member_id}}"),
        Step(step_id="s3_lookup", description="click Look Up Member", action=ActionType.CLICK,
             locator=_role_loc("Look Up Member button", "button", "Look Up Member")),
        Step(step_id="s4_type", description="open account type dropdown", action=ActionType.CLICK,
             locator=_loc("Account type control", "account_type_display")),
        Step(step_id="s5_type_option", description="choose Savings", action=ActionType.CLICK,
             locator=ElementLocator(description="Savings option", candidates=[
                 LocatorCandidate(strategy=LocatorStrategy.TEXT, value="Savings", confidence=0.6)]),
             risk=RiskLevel.SAFE),
        Step(step_id="s6_deposit", description="fill initial deposit", action=ActionType.FILL,
             locator=_loc("Initial deposit input", "initial_deposit"), value_template="{{initial_deposit}}"),
        Step(step_id="s7_terms", description="check terms", action=ActionType.CHECK,
             locator=_loc("Terms checkbox", "terms")),
        Step(step_id="s8_submit", description="submit account", action=ActionType.CLICK,
             locator=_loc("Submit button", "submit-account"), risk=RiskLevel.RISKY_IRREVERSIBLE),
        Step(step_id="s9_extract_acct", description="extract account number", action=ActionType.EXTRACT,
             locator=_loc("Account number cell", "account-number"), output_binding="account_number"),
    ]
    return CapabilityArtifact(
        artifact_id="mockbank__open-sub-account",
        name="mockbank: open a new sub-account",
        version="1.0.0",
        description="Open a new sub-account for a member and reach the confirmation screen",
        target=TargetSpec(app_id="mockbank", base_url="{{base_url}}"),
        input_params=[
            InputParam(name="base_url", type=ParamType.STRING, description="target base url"),
            InputParam(name="member_id", type=ParamType.STRING, description="member id to look up"),
            InputParam(name="initial_deposit", type=ParamType.STRING, description="initial deposit amount"),
        ],
        output_fields=[OutputField(name="account_number", type=ParamType.STRING, description="new account number", source_step_id="s9_extract_acct")],
        steps=steps,
        checkpoint=Checkpoint(description="confirmation panel visible", locator=_loc("confirmation panel", "confirmation-panel"), condition=CheckpointCondition.VISIBLE),
        risk_summary=RiskSummary(highest_risk=RiskLevel.RISKY_IRREVERSIBLE, irreversible_step_ids=["s8_submit"]),
        provenance=Provenance(recorded_by_model="hand-authored-test-fixture", discovery_run_id="n/a"),
    )


def _detect_business_outcome(page):
    loc = page.locator("#error-banner")
    if loc.count() and loc.is_visible():
        text = loc.inner_text()
        if "not found" in text.lower():
            return "MEMBER_NOT_FOUND", text
        if "terms" in text.lower():
            return "VALIDATION_ERROR_TERMS", text
        if "deposit" in text.lower():
            return "VALIDATION_ERROR_MIN_DEPOSIT", text
        return "UNKNOWN_BUSINESS_ERROR", text
    return None


def _dismiss_cookie_banner(page) -> bool:
    loc = page.locator("#cookie-accept")
    if loc.count() and loc.is_visible():
        loc.click()
        return True
    return False


def test_replay_success_happy_path(mock_server, page):
    artifact = build_open_account_artifact()
    evidence = EvidenceLogger.create("replay", base_dir="evidence/_test")
    allowlist = AllowlistConfig(allowed_domains=["127.0.0.1"])

    result = replay(
        artifact=artifact,
        input_args={"base_url": mock_server, "member_id": "12345", "initial_deposit": "100"},
        page=page, allowlist=allowlist, evidence=evidence,
        detect_business_outcome=_detect_business_outcome,
        interstitial_rules=[_dismiss_cookie_banner],
        interactive_escalation=False,
    )
    assert result.is_success(), result.message
    assert "account_number" in result.outputs
    assert len(result.outputs["account_number"]) == 10


def test_replay_business_outcome_member_not_found(mock_server, page):
    artifact = build_open_account_artifact()
    evidence = EvidenceLogger.create("replay", base_dir="evidence/_test")
    allowlist = AllowlistConfig(allowed_domains=["127.0.0.1"])

    result = replay(
        artifact=artifact,
        input_args={"base_url": mock_server, "member_id": "99999", "initial_deposit": "100"},
        page=page, allowlist=allowlist, evidence=evidence,
        detect_business_outcome=_detect_business_outcome,
        interstitial_rules=[_dismiss_cookie_banner],
        interactive_escalation=False,
    )
    assert result.kind.value == "business_outcome"
    assert result.business_code == "MEMBER_NOT_FOUND"


def test_replay_business_outcome_min_deposit(mock_server, page):
    artifact = build_open_account_artifact()
    evidence = EvidenceLogger.create("replay", base_dir="evidence/_test")
    allowlist = AllowlistConfig(allowed_domains=["127.0.0.1"])

    result = replay(
        artifact=artifact,
        input_args={"base_url": mock_server, "member_id": "12345", "initial_deposit": "1"},
        page=page, allowlist=allowlist, evidence=evidence,
        detect_business_outcome=_detect_business_outcome,
        interstitial_rules=[_dismiss_cookie_banner],
        interactive_escalation=False,
    )
    assert result.kind.value == "business_outcome"
    assert result.business_code == "VALIDATION_ERROR_MIN_DEPOSIT"


def test_allowlist_blocks_disallowed_domain(mock_server, page):
    artifact = build_open_account_artifact()
    evidence = EvidenceLogger.create("replay", base_dir="evidence/_test")
    allowlist = AllowlistConfig(allowed_domains=["not-127.example"])

    result = replay(
        artifact=artifact,
        input_args={"base_url": mock_server, "member_id": "12345", "initial_deposit": "100"},
        page=page, allowlist=allowlist, evidence=evidence,
        interactive_escalation=False,
    )
    assert result.kind.value == "hard_failure"
    assert "allowlist" in result.message.lower()


def test_irreversible_step_escalates_and_auto_approves_in_non_interactive_mode(mock_server, page):
    """
    With interactive_escalation=False, the RISKY_IRREVERSIBLE submit step
    still goes through raise_intervention()/handoff_to_human() -- it's just
    auto-approved rather than blocking on a terminal prompt. This proves the
    escalation seam actually fires on the real irreversible step, not just
    on contrived failures.
    """
    artifact = build_open_account_artifact()
    evidence = EvidenceLogger.create("replay", base_dir="evidence/_test")
    allowlist = AllowlistConfig(allowed_domains=["127.0.0.1"])

    result = replay(
        artifact=artifact,
        input_args={"base_url": mock_server, "member_id": "67890", "initial_deposit": "50"},
        page=page, allowlist=allowlist, evidence=evidence,
        detect_business_outcome=_detect_business_outcome,
        interstitial_rules=[_dismiss_cookie_banner],
        interactive_escalation=False,
    )
    assert result.is_success()
    assert result.escalation_id is not None

    import json
    events = [json.loads(l) for l in open(f"{evidence.dir}/run.jsonl")]
    kinds = [e["event"] for e in events]
    assert "escalation_raised" in kinds
    assert "control_transferred" in kinds
