"""
Safety & policy guardrails.

Three independent concerns, deliberately kept as separate small classes
rather than one god-object, since they're enforced at different points in
the pipeline:

  - Allowlist: enforced on every navigation/action, both during discovery
    (LLM-driven) and replay. Config-driven, not hardcoded, so a tenant's
    policy can differ.
  - RiskPolicy: classifies actions and decides what to do with
    risky/irreversible ones. Applied at record time (steps get tagged) and
    again at replay time (defense in depth -- an artifact could in
    principle be hand-edited).
  - Redactor: applied at the boundary where anything is about to be
    persisted (artifact JSON, evidence logs) -- never applied to the live
    in-memory values used to actually fill forms, since the automation
    still needs real data to do its job.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

from src.artifact.schema import ActionType, RiskLevel


# --------------------------------------------------------------------------
# Allowlist
# --------------------------------------------------------------------------

@dataclass
class AllowlistConfig:
    allowed_domains: list[str]              # e.g. ["demoqa.com", "*.demoqa.com"]
    allowed_route_prefixes: list[str] = field(default_factory=lambda: ["/"])
    allowed_actions: set[ActionType] = field(
        default_factory=lambda: set(ActionType)  # all actions allowed by default
    )

    def check_url(self, url: str) -> tuple[bool, str]:
        parsed = urlparse(url)
        host = parsed.netloc.split(":")[0]
        domain_ok = any(fnmatch.fnmatch(host, pat) for pat in self.allowed_domains)
        if not domain_ok:
            return False, f"domain {host!r} not in allowlist {self.allowed_domains}"
        route_ok = any(parsed.path.startswith(p) for p in self.allowed_route_prefixes)
        if not route_ok:
            return False, f"route {parsed.path!r} not in allowed prefixes {self.allowed_route_prefixes}"
        return True, ""

    def check_action(self, action: ActionType) -> tuple[bool, str]:
        if action not in self.allowed_actions:
            return False, f"action {action.value!r} not in allowed action types"
        return True, ""


class AllowlistViolation(Exception):
    pass


# --------------------------------------------------------------------------
# Risk policy
# --------------------------------------------------------------------------

class RiskDecision(str, Enum):
    PROCEED = "proceed"
    REQUIRE_CONFIRMATION = "require_confirmation"  # escalate to human before executing
    BLOCK = "block"


@dataclass
class RiskPolicy:
    """
    Conservative default: SAFE steps proceed automatically; anything
    RISKY_IRREVERSIBLE on an unapproved artifact requires human confirmation
    before it executes; on an *approved* artifact it still requires
    confirmation unless `auto_approve_irreversible` is explicitly set,
    which we deliberately default to False. RISKY_REVERSIBLE steps proceed
    automatically but are flagged in evidence for later review, since they
    can be undone if something goes wrong.

    Rationale: this is regulated financial data and the actions in question
    (opening an account, moving funds) are exactly the ones a bank would
    least want an unattended model to get wrong silently.
    """
    auto_approve_irreversible: bool = False

    def decide(self, risk: RiskLevel, artifact_approved: bool) -> RiskDecision:
        if risk == RiskLevel.SAFE:
            return RiskDecision.PROCEED
        if risk == RiskLevel.RISKY_REVERSIBLE:
            return RiskDecision.PROCEED
        # RISKY_IRREVERSIBLE
        if artifact_approved and self.auto_approve_irreversible:
            return RiskDecision.PROCEED
        return RiskDecision.REQUIRE_CONFIRMATION


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

_SECRET_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|ssn|social.?security|"
    r"credit.?card|card.?number|cvv|pin)",
    re.IGNORECASE,
)

# Coarse value-shape patterns as a second line of defense, in case a
# sensitive value ends up in a field not obviously named for it (e.g. an
# account number echoed into a generic "value" field).
_VALUE_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),          # SSN-shaped
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),         # card-number-shaped
]


def redact_value(value: str) -> str:
    if len(value) <= 4:
        return "***"
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


_IRREVERSIBLE_WORDS = re.compile(
    r"\b(submit|confirm|open account|transfer|delete|close account|approve|"
    r"send|pay|withdraw|deposit funds|finalize)\b",
    re.IGNORECASE,
)
_REVERSIBLE_MUTATION_WORDS = re.compile(
    r"\b(save draft|add|save|update|edit)\b", re.IGNORECASE,
)


def classify_risk(action: ActionType, element_name: str) -> RiskLevel:
    """
    Heuristic risk classification applied at record time (and re-applied at
    replay time as defense in depth). Read-only/navigational actions are
    always SAFE. CLICK/PRESS_KEY on an element whose accessible name looks
    like a submit/confirm/transfer/delete affordance is treated as
    IRREVERSIBLE -- conservatively, since the cost of an unnecessary
    confirmation prompt is much lower than the cost of silently opening an
    account or moving money. Everything else that mutates state (fill,
    select, check) is REVERSIBLE, since the mutation lives in an unsubmitted
    form and can simply be re-filled.
    """
    if action in (ActionType.NAVIGATE, ActionType.EXTRACT, ActionType.WAIT_FOR, ActionType.ASSERT_CHECKPOINT):
        return RiskLevel.SAFE
    if action in (ActionType.CLICK, ActionType.PRESS_KEY):
        if _IRREVERSIBLE_WORDS.search(element_name or ""):
            return RiskLevel.RISKY_IRREVERSIBLE
        return RiskLevel.SAFE
    if action in (ActionType.FILL, ActionType.SELECT, ActionType.CHECK):
        return RiskLevel.RISKY_REVERSIBLE if _REVERSIBLE_MUTATION_WORDS.search(element_name or "") else RiskLevel.SAFE
    return RiskLevel.SAFE


class Redactor:
    def __init__(self, pii_field_names: set[str] | None = None):
        self.pii_field_names = pii_field_names or set()

    def redact_dict(self, data: dict) -> dict:
        out = {}
        for k, v in data.items():
            if isinstance(v, dict):
                out[k] = self.redact_dict(v)
                continue
            if isinstance(v, str) and self._is_sensitive(k, v):
                out[k] = redact_value(v)
            else:
                out[k] = v
        return out

    def _is_sensitive(self, key: str, value: str) -> bool:
        if key in self.pii_field_names:
            return True
        if _SECRET_KEY_PATTERN.search(key):
            return True
        return any(p.search(value) for p in _VALUE_PATTERNS)
