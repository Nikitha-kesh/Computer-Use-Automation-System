"""
Resolves an artifact's ElementLocator (an ordered list of candidate
strategies) against a live page, trying candidates in order until one
identifies exactly one visible element. This is the mechanism that makes
replay resilient to small, legitimate UI variation without needing the LLM
back in the loop: the *artifact* encodes "try these signals, in this
order," and the *resolver* is dumb, deterministic code.

Also used symmetrically at discovery time (build_locator_from_perceived) so
the exact same candidate ranking logic underlies both recording and replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import Frame, Page

from src.agent.perception import PerceivedElement
from src.artifact.schema import ElementLocator, LocatorCandidate, LocatorStrategy


@dataclass
class ResolutionResult:
    locator: object  # playwright Locator, if found
    matched_strategy: Optional[LocatorStrategy]
    matched_candidate_index: Optional[int]
    error: Optional[str] = None


def _frame_for_path(page: Page, frame_path: list[str]) -> Optional[Frame]:
    frame = page.main_frame
    for name in frame_path:
        found = None
        for child in frame.child_frames:
            if (child.name or child.url) == name:
                found = child
                break
        if found is None:
            return None
        frame = found
    return frame


def _try_candidate(frame: Frame, cand: LocatorCandidate):
    try:
        if cand.strategy == LocatorStrategy.ROLE_NAME:
            role, _, name = cand.value.partition("::")
            loc = frame.get_by_role(role, name=name, exact=True)
        elif cand.strategy == LocatorStrategy.TEST_ID:
            loc = frame.locator(f'[data-testid="{cand.value}"], [data-test="{cand.value}"], #{cand.value}')
        elif cand.strategy == LocatorStrategy.LABEL_TEXT:
            loc = frame.get_by_label(cand.value, exact=True)
        elif cand.strategy == LocatorStrategy.CSS:
            loc = frame.locator(cand.value)
        elif cand.strategy == LocatorStrategy.XPATH:
            loc = frame.locator(f"xpath={cand.value}")
        elif cand.strategy == LocatorStrategy.TEXT:
            loc = frame.get_by_text(cand.value, exact=True)
        else:
            return None
        if loc.count() == 1:
            return loc
        return None
    except Exception:
        return None


def resolve(page: Page, element_locator: ElementLocator) -> ResolutionResult:
    # Candidates may each carry their own frame_path (in principle every
    # candidate targets the same element, but recorded independently) --
    # in practice we use the first candidate's frame_path for all of them,
    # since they describe the same element.
    frame_path = element_locator.candidates[0].frame_path
    frame = _frame_for_path(page, frame_path)
    if frame is None:
        return ResolutionResult(None, None, None, error=f"frame path not found: {frame_path}")

    for i, cand in enumerate(element_locator.candidates):
        loc = _try_candidate(frame, cand)
        if loc is not None:
            return ResolutionResult(loc, cand.strategy, i)

    return ResolutionResult(
        None, None, None,
        error=f"no candidate resolved uniquely for '{element_locator.description}' "
              f"(tried {[c.strategy.value for c in element_locator.candidates]})",
    )


def build_locator_from_perceived(el: PerceivedElement) -> ElementLocator:
    """
    Build the ranked candidate list for a freshly-perceived element, in the
    priority order documented on LocatorStrategy. Only strategies that are
    actually viable for this element are included.
    """
    candidates: list[LocatorCandidate] = []

    if el.name:
        candidates.append(LocatorCandidate(
            strategy=LocatorStrategy.ROLE_NAME,
            value=f"{el.role}::{el.name}",
            frame_path=el.frame_path,
            confidence=0.9,
        ))
    if el.test_id:
        candidates.append(LocatorCandidate(
            strategy=LocatorStrategy.TEST_ID,
            value=el.test_id,
            frame_path=el.frame_path,
            confidence=0.85,
        ))
    if el.name and el.tag in ("input", "textarea", "select"):
        candidates.append(LocatorCandidate(
            strategy=LocatorStrategy.LABEL_TEXT,
            value=el.name,
            frame_path=el.frame_path,
            confidence=0.7,
        ))
    if el.dom_id:
        candidates.append(LocatorCandidate(
            strategy=LocatorStrategy.CSS,
            value=f"#{el.dom_id}",
            frame_path=el.frame_path,
            confidence=0.6,
        ))
    candidates.append(LocatorCandidate(
        strategy=LocatorStrategy.CSS,
        value=el.css,
        frame_path=el.frame_path,
        confidence=0.3,
    ))
    if el.name and el.tag in ("button", "a"):
        candidates.append(LocatorCandidate(
            strategy=LocatorStrategy.TEXT,
            value=el.name,
            frame_path=el.frame_path,
            confidence=0.5,
        ))

    return ElementLocator(
        description=f"{el.role} \"{el.name}\"" if el.name else f"{el.role} element",
        candidates=candidates,
    )
