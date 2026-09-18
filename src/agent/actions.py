"""
Action execution primitives.

Deliberately the *same* code path is used to execute an action during a
live LLM-driven discovery run and during deterministic replay. What
differs between the two contexts is only *who decides* which action to run
next (the LLM vs. a recorded Step) -- never *how* an action is carried out.
That symmetry is what makes "replay does exactly what was recorded"
actually true.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from playwright.sync_api import Page, TimeoutError as PWTimeoutError

from src.artifact.schema import ActionType, CheckpointCondition, ElementLocator, LocatorStrategy
from src.replay.locator_resolver import resolve


@dataclass
class ActionExecutionResult:
    ok: bool
    error: Optional[str] = None
    extracted_value: Any = None
    matched_strategy: Optional[LocatorStrategy] = None
    matched_candidate_index: Optional[int] = None


def _resolve_or_fail(page: Page, locator: ElementLocator, timeout_ms: int) -> tuple[Optional[object], ActionExecutionResult]:
    res = resolve(page, locator)
    if res.locator is None:
        return None, ActionExecutionResult(ok=False, error=res.error)
    try:
        res.locator.wait_for(state="visible", timeout=timeout_ms)
    except PWTimeoutError:
        return None, ActionExecutionResult(
            ok=False,
            error=f"element resolved ({res.matched_strategy}) but never became visible within {timeout_ms}ms",
            matched_strategy=res.matched_strategy,
            matched_candidate_index=res.matched_candidate_index,
        )
    return res.locator, ActionExecutionResult(ok=True, matched_strategy=res.matched_strategy, matched_candidate_index=res.matched_candidate_index)


def execute_action(
    page: Page,
    action: ActionType,
    locator: Optional[ElementLocator] = None,
    value: Optional[str] = None,
    timeout_ms: int = 5000,
) -> ActionExecutionResult:
    try:
        if action == ActionType.NAVIGATE:
            page.goto(value, timeout=timeout_ms, wait_until="domcontentloaded")
            return ActionExecutionResult(ok=True)

        if locator is None:
            return ActionExecutionResult(ok=False, error=f"action {action.value} requires a locator")

        el, base = _resolve_or_fail(page, locator, timeout_ms)
        if el is None:
            return base

        if action == ActionType.CLICK:
            el.click(timeout=timeout_ms)
        elif action == ActionType.FILL:
            el.fill(value or "", timeout=timeout_ms)
        elif action == ActionType.SELECT:
            _select(el, value or "", timeout_ms)
        elif action == ActionType.CHECK:
            el.check(timeout=timeout_ms)
        elif action == ActionType.PRESS_KEY:
            el.press(value or "Enter", timeout=timeout_ms)
        elif action == ActionType.WAIT_FOR:
            el.wait_for(state="visible", timeout=timeout_ms)
        elif action == ActionType.EXTRACT:
            extracted = el.input_value() if _is_input_like(el) else el.inner_text()
            base.extracted_value = extracted.strip() if isinstance(extracted, str) else extracted
        elif action == ActionType.ASSERT_CHECKPOINT:
            pass  # checkpoints are asserted via assert_checkpoint(), not through execute_action
        else:
            return ActionExecutionResult(ok=False, error=f"unsupported action {action.value}")

        return base

    except PWTimeoutError as e:
        return ActionExecutionResult(ok=False, error=f"timeout: {e}")
    except Exception as e:  # noqa: BLE001
        return ActionExecutionResult(ok=False, error=f"{type(e).__name__}: {e}")


def _is_input_like(locator) -> bool:
    try:
        tag = locator.evaluate("el => el.tagName.toLowerCase()")
        return tag in ("input", "textarea", "select")
    except Exception:
        return False


def _select(locator, value: str, timeout_ms: int) -> None:
    """
    Handles both native <select> and the common "custom combobox" pattern
    (click to open, click the matching option by text) used by many
    component libraries that don't render a real <select> -- exactly the
    kind of non-native control called out in the brief as typical of
    heterogeneous/legacy surfaces.
    """
    tag = locator.evaluate("el => el.tagName.toLowerCase()")
    if tag == "select":
        try:
            locator.select_option(label=value, timeout=timeout_ms)
            return
        except Exception:
            locator.select_option(value=value, timeout=timeout_ms)
            return
    # Custom dropdown: click to open, then click the option by visible text
    # in the same page (options are frequently portaled to <body>).
    locator.click(timeout=timeout_ms)
    page = locator.page
    option = page.get_by_text(value, exact=True).last
    option.wait_for(state="visible", timeout=timeout_ms)
    option.click(timeout=timeout_ms)


def assert_checkpoint(
    page: Page,
    locator: Optional[ElementLocator],
    condition: CheckpointCondition,
    expected_value: Optional[str],
    timeout_ms: int = 8000,
) -> ActionExecutionResult:
    try:
        if condition == CheckpointCondition.URL_MATCHES:
            page.wait_for_url(f"**{expected_value}**", timeout=timeout_ms)
            return ActionExecutionResult(ok=True)

        if locator is None:
            return ActionExecutionResult(ok=False, error="checkpoint requires a locator for this condition")

        el, base = _resolve_or_fail(page, locator, timeout_ms)
        if el is None:
            return base

        if condition == CheckpointCondition.VISIBLE:
            return base

        text = el.inner_text(timeout=timeout_ms).strip()
        if condition == CheckpointCondition.TEXT_EQUALS:
            ok = text == (expected_value or "")
        elif condition == CheckpointCondition.TEXT_CONTAINS:
            ok = (expected_value or "") in text
        else:
            return ActionExecutionResult(ok=False, error=f"unsupported checkpoint condition {condition}")

        if not ok:
            return ActionExecutionResult(
                ok=False,
                error=f"checkpoint text mismatch: expected {expected_value!r}, observed {text!r}",
            )
        return base

    except PWTimeoutError as e:
        return ActionExecutionResult(ok=False, error=f"checkpoint timeout: {e}")
    except Exception as e:  # noqa: BLE001
        return ActionExecutionResult(ok=False, error=f"{type(e).__name__}: {e}")
