"""
This is the test that matters most for honesty about what's actually been
run vs. designed. Everything in `src/agent/loop.py` (the discovery loop),
`src/artifact/recorder.py` (transcript -> artifact), perception, action
execution, risk classification, and escalation is exercised for REAL here
-- real Playwright browser, real mock app, real locator resolution, a real
irreversible-step escalation -- with exactly one thing faked: which tool
call comes back at each turn, via `FakeAgentClient` standing in for
`DiscoveryAgentClient` (the actual Anthropic-backed client, untouched,
still lives in src/agent/llm_client.py and is what `discover` uses by
default).

FakeAgentClient reads the *real* rendered element list produced by the
*real* perception layer each turn and locates elements by matching on
their accessible name -- it does not know indices in advance -- which is
the same interface constraint the real LLM operates under (indices shift
as the page changes). What it does NOT do is any actual reasoning: the
sequence of "which field to fill next" is hand-scripted, standing in for
what a real model would decide.

This closes the gap between "the discovery loop compiles" and "the
discovery loop, as actually written, drives a browser through a multi-step
form, gets escalated on the irreversible submit, and produces an artifact
that then replays successfully" -- everything except the live model call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from src.agent.llm_client import LLMTurn, ToolCall
from src.agent.loop import run_discovery
from src.artifact.recorder import build_artifact
from src.artifact.store import ArtifactStore
from src.evidence.logger import EvidenceLogger
from src.replay.engine import replay
from src.safety.policy import AllowlistConfig


@dataclass
class ScriptedStep:
    tool: str
    find: Optional[str] = None   # substring to match against the rendered element list's accessible name
    input_extra: dict = field(default_factory=dict)


SCRIPT = [
    ScriptedStep(tool="navigate", input_extra={}),  # url filled in by FakeAgentClient.start()
    ScriptedStep(tool="click", find="Accept"),                                   # dismiss cookie banner
    ScriptedStep(tool="fill", find="Member ID", input_extra={"value": "12345", "param_name": "member_id"}),
    ScriptedStep(tool="click", find="Look Up Member"),
    ScriptedStep(tool="click", find="Account Type"),                             # open custom dropdown
    ScriptedStep(tool="click", find="Savings"),                                  # pick option
    ScriptedStep(tool="fill", find="Initial Deposit", input_extra={"value": "100", "param_name": "initial_deposit"}),
    ScriptedStep(tool="check", find="I confirm the account holder"),
    ScriptedStep(tool="click", find="Submit"),
    ScriptedStep(tool="extract", find="Account Number value", input_extra={"output_name": "account_number"}),
    ScriptedStep(tool="finish", find="Account confirmation panel", input_extra={
        "success": True, "checkpoint_expected_text": None,
        "summary": "Opened a new sub-account and reached the confirmation screen.",
    }),
]

_INDEX_RE = re.compile(r"^\[(\d+)\]\s+\S+\s+\"([^\"]*)\"")


class FakeAgentClient:
    """Same call surface as DiscoveryAgentClient; scripted instead of model-backed."""

    def __init__(self, script: list[ScriptedStep]):
        self.script = list(script)
        self.target_url = None
        self._tool_seq = 0

    def start(self, goal: str, target_url: str, declared_params: list[dict]) -> None:
        self.target_url = target_url

    def observe_and_decide(self, elements_description: str, last_action_result: Optional[str] = None) -> LLMTurn:
        if not self.script:
            return LLMTurn(tool_call=None, raw_text="script exhausted", stop_reason="end_turn")
        step = self.script.pop(0)
        self._tool_seq += 1
        tc_id = f"fake_{self._tool_seq}"

        if step.tool == "navigate":
            return LLMTurn(ToolCall(tc_id, "navigate", {"url": self.target_url}), "scripted navigate", "tool_use")
        if step.tool == "finish":
            idx = self._find_index(elements_description, step.find) if step.find else None
            payload = dict(step.input_extra)
            if idx is not None:
                payload["checkpoint_element_index"] = idx
            return LLMTurn(ToolCall(tc_id, "finish", payload), "scripted finish", "tool_use")

        idx = self._find_index(elements_description, step.find)
        if idx is None:
            raise AssertionError(f"FakeAgentClient script step {step} could not find element matching {step.find!r} in:\n{elements_description}")
        payload = {"element_index": idx, **step.input_extra}
        return LLMTurn(ToolCall(tc_id, step.tool, payload), f"scripted {step.tool}", "tool_use")

    def report_tool_result(self, tool_call_id: str, result_text: str, is_error: bool = False) -> None:
        pass  # fake client doesn't need to track conversation state

    @staticmethod
    def _find_index(elements_description: str, needle: str) -> Optional[int]:
        for line in elements_description.splitlines():
            m = _INDEX_RE.match(line)
            if m and needle.lower() in m.group(2).lower():
                return int(m.group(1))
        return None


def test_full_discovery_to_replay_pipeline_with_scripted_model(mock_server, page):
    """
    Runs the REAL run_discovery loop (perception, safety, risk
    classification, escalation, action execution all real) against the
    REAL mock app, with only the model call swapped for a deterministic
    script. Then feeds the resulting transcript through the REAL recorder
    to build an artifact, saves it, and replays that artifact through the
    REAL deterministic replay engine -- proving discovery output is
    actually consumable by replay, not just structurally valid.
    """
    allowlist = AllowlistConfig(allowed_domains=["127.0.0.1"])
    evidence = EvidenceLogger.create("discovery", base_dir="evidence/_test")
    fake_llm = FakeAgentClient(SCRIPT)

    outcome = run_discovery(
        page=page,
        goal="Open a new sub-account for member 12345 and reach the confirmation screen",
        target_url=mock_server + "/",
        app_id="mockbank",
        declared_params=[
            {"name": "member_id", "type": "string", "description": "member id"},
            {"name": "initial_deposit", "type": "string", "description": "deposit amount"},
        ],
        allowlist=allowlist,
        evidence=evidence,
        max_steps=20,
        interactive_escalation=False,  # auto-approve the irreversible "Submit" escalation
        llm_client=fake_llm,
    )

    assert outcome.success, outcome.summary
    assert len(outcome.transcript) > 0

    # The irreversible submit step must have actually triggered a real escalation.
    import json
    events = [json.loads(l) for l in open(f"{evidence.dir}/run.jsonl")]
    kinds = [e["event"] for e in events]
    assert "escalation_raised" in kinds
    assert "control_transferred" in kinds

    artifact = build_artifact(
        outcome=outcome,
        goal="Open a new sub-account for member 12345 and reach the confirmation screen",
        app_id="mockbank",
        base_url=mock_server,
        target_url=mock_server + "/",
        discovery_run_id=evidence.run_id,
        model_name="fake-scripted-model-for-testing",
    )

    assert artifact.input_params, "expected member_id/initial_deposit to be parameterized"
    param_names = {p.name for p in artifact.input_params}
    assert "member_id" in param_names
    assert "initial_deposit" in param_names
    assert artifact.output_fields, "expected account_number to be captured as an output"
    assert any(o.name == "account_number" for o in artifact.output_fields)
    assert artifact.risk_summary.irreversible_step_ids, "expected the Submit click to be classified irreversible"

    store = ArtifactStore("artifacts/_test")
    store.save(artifact)

    # Now replay the artifact that discovery actually produced -- with DIFFERENT
    # param values than discovery used, against a fresh page/session, proving reuse.
    page2_evidence = EvidenceLogger.create("replay", base_dir="evidence/_test")
    result = replay(
        artifact=artifact,
        input_args={"member_id": "67890", "initial_deposit": "50"},
        page=page,
        allowlist=allowlist,
        evidence=page2_evidence,
        interactive_escalation=False,
    )
    assert result.is_success(), result.message
    assert "account_number" in result.outputs
