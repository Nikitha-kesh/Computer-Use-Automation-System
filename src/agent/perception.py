"""
Perception layer: turns the live page into a compact, structured list of
interactive elements the LLM can reason about and reference by index.

Why not screenshot + pixel coordinates as the primary channel? Coordinates
are the *most* general (work on anything you can see), but they are also
the least robust to replay later -- a control that moves 20px breaks a
coordinate-based artifact even though nothing meaningful changed. Since the
brief's own environment description says these are enterprise apps with
"fairly consistent" UIs where the goal is durable, replayable artifacts, we
bias toward structural signals (accessibility role, name, label, test id)
and keep bounding boxes only as a last-resort fallback / for screenshot
annotation, matching the "bias toward an approach that would still work
when there's no clean DOM" instruction in the brief -- accessibility roles
are available even on non-semantic legacy markup and on native desktop
apps (see REPORT.md section 4), whereas a clean CSS selector often is not.

The extraction itself runs as injected JS so it works identically inside
iframes/framesets by walking each frame separately (frame_path is recorded
per element), which is the seam legacy web apps need.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Frame, Page

_EXTRACT_JS = r"""
() => {
  const INTERACTIVE_SELECTOR = [
    'input:not([type=hidden])', 'textarea', 'select', 'button',
    'a[href]', '[role]', '[tabindex]', '[contenteditable="true"]'
  ].join(',');

  function accessibleName(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const parts = labelledby.split(/\s+/).map(id => {
        const t = document.getElementById(id);
        return t ? t.textContent.trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return lbl.textContent.trim();
    }
    const parentLabel = el.closest('label');
    if (parentLabel) return parentLabel.textContent.trim();
    if (el.placeholder) return el.placeholder.trim();
    if (el.tagName === 'BUTTON' || el.tagName === 'A') return el.textContent.trim();
    if (el.getAttribute('value') && el.tagName === 'INPUT') return el.getAttribute('value').trim();
    return (el.textContent || '').trim().slice(0, 80);
  }

  function implicitRole(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'submit' || t === 'button') return 'button';
      return 'textbox';
    }
    return tag;
  }

  function cssPath(el) {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      let sel = node.tagName.toLowerCase();
      if (node.classList.length) {
        sel += '.' + Array.from(node.classList).slice(0, 2).map(c => CSS.escape(c)).join('.');
      }
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (siblings.length > 1) {
          sel += `:nth-of-type(${siblings.indexOf(node) + 1})`;
        }
      }
      parts.unshift(sel);
      node = parent;
    }
    return parts.join(' > ');
  }

  const els = Array.from(document.querySelectorAll(INTERACTIVE_SELECTOR));
  return els.map((el, i) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const visible = rect.width > 0 && rect.height > 0 &&
      style.visibility !== 'hidden' && style.display !== 'none';
    return {
      index: i,
      tag: el.tagName.toLowerCase(),
      role: implicitRole(el),
      name: accessibleName(el),
      test_id: el.getAttribute('data-testid') || el.getAttribute('data-test') || null,
      dom_id: el.id || null,
      input_type: el.getAttribute('type') || null,
      css: cssPath(el),
      visible: visible,
      disabled: !!el.disabled,
      current_value: (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') ? el.value :
                     (el.tagName === 'SELECT' ? el.value : null),
      checked: el.type === 'checkbox' || el.type === 'radio' ? !!el.checked : null,
      bbox: visible ? {x: rect.x, y: rect.y, w: rect.width, h: rect.height} : null,
    };
  }).filter(e => e.visible);
}
"""


@dataclass
class PerceivedElement:
    index: int
    tag: str
    role: str
    name: str
    test_id: str | None
    dom_id: str | None
    input_type: str | None
    css: str
    disabled: bool
    current_value: Any
    checked: Any
    bbox: dict | None
    frame_path: list[str]


def _walk_frames(page: Page) -> list[tuple[Frame, list[str]]]:
    """Return (frame, path) for the main frame and every descendant frame."""
    out: list[tuple[Frame, list[str]]] = [(page.main_frame, [])]

    def recurse(frame: Frame, path: list[str]):
        for child in frame.child_frames:
            name = child.name or (child.url or "frame")
            child_path = path + [name]
            out.append((child, child_path))
            recurse(child, child_path)

    recurse(page.main_frame, [])
    return out


def perceive(page: Page) -> list[PerceivedElement]:
    """
    Snapshot every interactive, visible element across the main frame and
    all nested frames. Returns a flat, globally-indexed list -- the index
    is what the LLM references in tool calls, and what gets translated back
    into a durable ElementLocator by recorder.py.
    """
    elements: list[PerceivedElement] = []
    global_index = 0
    for frame, path in _walk_frames(page):
        try:
            raw = frame.evaluate(_EXTRACT_JS)
        except Exception:
            continue
        for item in raw:
            elements.append(
                PerceivedElement(
                    index=global_index,
                    tag=item["tag"],
                    role=item["role"],
                    name=item["name"],
                    test_id=item["test_id"],
                    dom_id=item["dom_id"],
                    input_type=item["input_type"],
                    css=item["css"],
                    disabled=item["disabled"],
                    current_value=item["current_value"],
                    checked=item["checked"],
                    bbox=item["bbox"],
                    frame_path=path,
                )
            )
            global_index += 1
    return elements


def render_for_llm(elements: list[PerceivedElement], max_elements: int = 120) -> str:
    """Compact textual description of the page's interactive surface."""
    lines = []
    for e in elements[:max_elements]:
        frame_tag = f" (frame:{'/'.join(e.frame_path)})" if e.frame_path else ""
        state = []
        if e.disabled:
            state.append("disabled")
        if e.checked is not None:
            state.append(f"checked={e.checked}")
        if e.current_value:
            state.append(f"value={e.current_value!r}")
        state_str = f" [{', '.join(state)}]" if state else ""
        lines.append(f"[{e.index}] {e.role} \"{e.name}\"{frame_tag}{state_str}")
    if len(elements) > max_elements:
        lines.append(f"... ({len(elements) - max_elements} more elements truncated)")
    return "\n".join(lines)
