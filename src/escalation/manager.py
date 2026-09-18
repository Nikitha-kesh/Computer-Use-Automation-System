"""
Escalation & handoff.

Scope, per the brief: a full real-time co-browsing console is out of
scope; what's required is a *real* handoff mechanism and control-transfer
model, with the operator UI itself allowed to be a mock. This module is
that mechanism:

  - `raise_intervention` builds an InterventionRequest carrying exactly the
    context a person needs to act (which capability/goal, current step,
    screenshot, and why the system stopped) and logs it as evidence.
  - `handoff_to_human` is the control-transfer seam: it flips a single
    `control` flag, gives a human a command loop that operates the *same*
    live Playwright `page` object the automation was just using (not a new
    session -- there is exactly one browser context for the whole run), and
    every command the human issues is itself logged as evidence. When the
    human signals they're done, control flips back and the caller (agent
    loop or replay engine) decides how to proceed (resume, retry the
    step, or treat as a hard failure) based on the boolean/verdict returned.

The operator console here is intentionally a bare CLI (`operator>` prompt)
rather than a built UI -- that's the piece the brief explicitly allows to
be mocked. What's real is: the pause, the same-session handoff, the
logged human actions, and the resume.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from playwright.sync_api import Page

from src.evidence.logger import EvidenceLogger


class ControlHolder(str, Enum):
    AGENT = "agent"
    HUMAN = "human"


@dataclass
class InterventionRequest:
    request_id: str
    capability_name: str
    context_ref: str          # goal text (discovery) or artifact_id (replay)
    step_id: Optional[str]
    reason: str
    screenshot_path: Optional[str]
    current_url: Optional[str]
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class EscalationManager:
    def __init__(self, evidence_logger: EvidenceLogger, page: Page, interactive: bool = True):
        self.evidence_logger = evidence_logger
        self.page = page
        self.interactive = interactive  # False = auto-confirm for unattended demo/CI runs
        self.control = ControlHolder.AGENT

    def raise_intervention(self, capability_name: str, context_ref: str, reason: str, step_id: Optional[str] = None) -> InterventionRequest:
        screenshot = self.evidence_logger.save_screenshot(self.page, "escalation")
        try:
            url = self.page.url
        except Exception:
            url = None
        req = InterventionRequest(
            request_id=f"esc-{uuid.uuid4().hex[:8]}",
            capability_name=capability_name,
            context_ref=context_ref,
            step_id=step_id,
            reason=reason,
            screenshot_path=screenshot,
            current_url=url,
        )
        self.evidence_logger.log_event("escalation_raised", **asdict(req))
        return req

    def handoff_to_human(self, request: InterventionRequest) -> bool:
        """
        Pause automation, transfer control of the live session to a human,
        run the operator loop, then transfer control back. Returns True if
        the human approved/resolved the situation, False if they rejected it
        (caller should treat as a hard failure / abort).
        """
        self.control = ControlHolder.HUMAN
        self.evidence_logger.log_event("control_transferred", to="human", request_id=request.request_id)

        if not self.interactive:
            self.evidence_logger.log_event(
                "human_action", request_id=request.request_id,
                action="auto_approve", note="non-interactive mode (--no-interactive); simulating operator approval",
            )
            approved = True
        else:
            approved = self._operator_console(request)

        self.control = ControlHolder.AGENT
        self.evidence_logger.log_event("control_transferred", to="agent", request_id=request.request_id, approved=approved)
        return approved

    # -- bare operator console -------------------------------------------------
    def _operator_console(self, request: InterventionRequest) -> bool:
        print("\n" + "=" * 70)
        print("HUMAN INTERVENTION REQUESTED")
        print(f"  capability : {request.capability_name}")
        print(f"  context    : {request.context_ref}")
        print(f"  step       : {request.step_id}")
        print(f"  reason     : {request.reason}")
        print(f"  url        : {request.current_url}")
        print(f"  screenshot : {request.screenshot_path}")
        print("=" * 70)
        print("You now control the LIVE browser session (same page, not a new one).")
        print("Commands: state | screenshot | click <css> | fill <css> <value> | approve | reject")
        print("=" * 70)

        while True:
            try:
                raw = input("operator> ").strip()
            except EOFError:
                self.evidence_logger.log_event("human_action", request_id=request.request_id, action="eof_default_reject")
                return False
            if not raw:
                continue
            parts = raw.split(maxsplit=2)
            cmd = parts[0].lower()

            if cmd == "approve":
                self.evidence_logger.log_event("human_action", request_id=request.request_id, action="approve")
                return True
            if cmd == "reject":
                self.evidence_logger.log_event("human_action", request_id=request.request_id, action="reject")
                return False
            if cmd == "state":
                try:
                    print(f"  url={self.page.url} title={self.page.title()}")
                except Exception as e:
                    print(f"  (could not read state: {e})")
                continue
            if cmd == "screenshot":
                path = self.evidence_logger.save_screenshot(self.page, "operator_manual")
                self.evidence_logger.log_event("human_action", request_id=request.request_id, action="screenshot", path=path)
                print(f"  saved: {path}")
                continue
            if cmd == "click" and len(parts) >= 2:
                selector = parts[1]
                try:
                    self.page.locator(selector).first.click(timeout=5000)
                    self.evidence_logger.log_event("human_action", request_id=request.request_id, action="click", selector=selector)
                    print("  clicked.")
                except Exception as e:
                    print(f"  click failed: {e}")
                continue
            if cmd == "fill" and len(parts) >= 3:
                selector, value = parts[1], parts[2]
                try:
                    self.page.locator(selector).first.fill(value, timeout=5000)
                    self.evidence_logger.log_event("human_action", request_id=request.request_id, action="fill", selector=selector)
                    print("  filled.")
                except Exception as e:
                    print(f"  fill failed: {e}")
                continue

            print("  unrecognized command.")
