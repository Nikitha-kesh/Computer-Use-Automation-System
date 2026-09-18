"""
Thin wrapper around the Anthropic Messages API configured for a
computer-use-style tool-calling loop. Kept deliberately small: one method
that takes the running conversation + current page perception and returns
the next tool call the model wants to make.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

import anthropic

DEFAULT_MODEL = os.environ.get("CUA_MODEL", "claude-sonnet-4-5-20250929")

TOOLS = [
    {
        "name": "navigate",
        "description": "Go to a URL. Only use for the initial navigation or if you need to recover to a known page.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "click",
        "description": "Click the interactive element with the given index (from the element list you were shown).",
        "input_schema": {
            "type": "object",
            "properties": {"element_index": {"type": "integer"}},
            "required": ["element_index"],
        },
    },
    {
        "name": "fill",
        "description": (
            "Type a value into a text input/textarea. If this value logically corresponds to one of the "
            "declared input parameters for this capability (e.g. a member's first name), set param_name to "
            "that parameter's name so it gets parameterized in the recorded artifact instead of hardcoded."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_index": {"type": "integer"},
                "value": {"type": "string"},
                "param_name": {"type": "string", "description": "optional; snake_case input param name"},
            },
            "required": ["element_index", "value"],
        },
    },
    {
        "name": "select",
        "description": "Choose an option in a dropdown/combobox (native <select> or custom listbox) by its visible text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_index": {"type": "integer"},
                "value": {"type": "string"},
                "param_name": {"type": "string"},
            },
            "required": ["element_index", "value"],
        },
    },
    {
        "name": "check",
        "description": "Check a checkbox or select a radio button.",
        "input_schema": {
            "type": "object",
            "properties": {"element_index": {"type": "integer"}},
            "required": ["element_index"],
        },
    },
    {
        "name": "press_key",
        "description": "Press a keyboard key while an element is focused (e.g. Escape, Enter).",
        "input_schema": {
            "type": "object",
            "properties": {"element_index": {"type": "integer"}, "key": {"type": "string"}},
            "required": ["element_index", "key"],
        },
    },
    {
        "name": "extract",
        "description": "Read the current text/value of an element and record it as a named output of this capability.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_index": {"type": "integer"},
                "output_name": {"type": "string", "description": "snake_case output field name"},
            },
            "required": ["element_index", "output_name"],
        },
    },
    {
        "name": "wait",
        "description": "Wait for the page to settle (e.g. after a submit) before observing again.",
        "input_schema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
        },
    },
    {
        "name": "request_human_help",
        "description": "Call this if you are stuck, blocked, or about to take an irreversible action you are not confident about. Explain why.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
    {
        "name": "finish",
        "description": "Call this once the goal has been reached and you have identified a reliable checkpoint element that confirms it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "checkpoint_element_index": {"type": "integer", "description": "element that proves the goal state was reached"},
                "checkpoint_expected_text": {"type": "string", "description": "text expected in/near that element"},
                "summary": {"type": "string"},
            },
            "required": ["success", "summary"],
        },
    },
]

SYSTEM_PROMPT = """You are a careful back-office automation operator for a bank. You are driving a \
real web application to accomplish a stated goal, one action at a time. You will be shown a numbered \
list of the currently visible interactive elements after every action; element indices change between \
turns as the page changes, so always act on the list you were most recently shown, never a stale index.

Rules:
- Take exactly one tool action per turn.
- Prefer the most semantically meaningful element (labelled inputs, named buttons) over guessing.
- When filling in a field whose value represents a caller-supplied parameter (a name, an ID, an amount, \
a date, etc. that would plausibly differ on a future invocation of this same capability), set param_name \
to a short snake_case name for it. Reuse the exact same param_name every time the same logical input is used.
- When you reach a page/state that reads back or confirms data you extracted or entered, use `extract` on \
the specific element containing each piece of output data the caller would want back, with a clear \
output_name.
- If you hit something you cannot safely resolve on your own (an unexpected dialog, an error you don't \
understand, an action that looks irreversible and high-stakes, or you've made no progress in several turns), \
call request_human_help with a clear reason instead of guessing.
- Call finish only once you have reached a state that reliably confirms the goal was achieved, and identify \
the single best checkpoint element/text that a later, non-LLM replay could check to verify success.
"""


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMTurn:
    tool_call: Optional[ToolCall]
    raw_text: str
    stop_reason: str


class DiscoveryAgentClient:
    def __init__(self, model: str = DEFAULT_MODEL, api_key: Optional[str] = None):
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key)
        self.messages: list[dict] = []

    def start(self, goal: str, target_url: str, declared_params: list[dict]) -> None:
        param_desc = "\n".join(f"- {p['name']} ({p['type']}): {p['description']}" for p in declared_params) or "(none declared up front; infer from the goal)"
        self.messages = [{
            "role": "user",
            "content": (
                f"Goal: {goal}\n"
                f"Starting URL: {target_url}\n\n"
                f"Known/likely input parameters for this capability:\n{param_desc}\n\n"
                "Begin by navigating to the starting URL."
            ),
        }]

    def observe_and_decide(self, elements_description: str, last_action_result: Optional[str] = None) -> LLMTurn:
        content = f"Visible interactive elements:\n{elements_description}"
        if last_action_result:
            content = f"Result of your last action: {last_action_result}\n\n{content}"
        self.messages.append({"role": "user", "content": content})

        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            tool_choice={"type": "any"},
            messages=self.messages,
        )

        assistant_content = [block.model_dump() for block in resp.content]
        self.messages.append({"role": "assistant", "content": assistant_content})

        text_parts = [b["text"] for b in assistant_content if b.get("type") == "text"]
        tool_blocks = [b for b in assistant_content if b.get("type") == "tool_use"]

        tool_call = None
        if tool_blocks:
            b = tool_blocks[0]
            tool_call = ToolCall(id=b["id"], name=b["name"], input=b["input"])

        return LLMTurn(tool_call=tool_call, raw_text="\n".join(text_parts), stop_reason=resp.stop_reason)

    def report_tool_result(self, tool_call_id: str, result_text: str, is_error: bool = False) -> None:
        self.messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": result_text,
                "is_error": is_error,
            }],
        })
