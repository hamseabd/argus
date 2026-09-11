"""Hooks that keep the agent read-only and leave an audit trail.

allowed_tools already limits what the model can call. The PreToolUse hook
is defense in depth: if a mutating tool is ever requested it is denied with
a reason the model can read, so it does not retry. The other hooks only
observe: they log every tool call, count subagent starts so the pipeline
can tell whether the lead delegated as instructed, and count rejected
structured outputs so a review that only validated after several attempts
is visible in the metrics.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import HookContext, HookMatcher
from claude_agent_sdk.types import HookEvent

from argus.agent.tools import GIT_HISTORY_TOOL_NAME
from argus.telemetry import get_logger

DENIED_TOOLS = frozenset(
    {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "WebFetch", "WebSearch"}
)
DENIED_TOOL_MATCHER = "|".join(sorted(DENIED_TOOLS))
ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob", "Agent", GIT_HISTORY_TOOL_NAME)
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
"""The SDK's internal tool that validates the final answer against the output schema.

The name comes from the bundled CLI, not the Python package, so nothing here
can pin it. If it ever changes the count silently reads zero; the schema's
length floor, not this counter, is what keeps a placeholder out of a review.
"""

_INPUT_SUMMARY_CHARS = 200

Hook = Callable[[dict[str, Any], str | None, HookContext], Awaitable[dict[str, Any]]]


@dataclass
class HookState:
    """Counters one query's hooks update; read by the runner for metrics."""

    denied: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    output_rejections: int = 0
    subagents_started: int = 0
    subagents_stopped: int = 0


def deny_mutating_tools(state: HookState) -> Hook:
    async def hook(data: dict[str, Any], _tool_use_id: str | None, _ctx: HookContext) -> dict:
        name = data.get("tool_name", "")
        if name not in DENIED_TOOLS:
            return {}
        state.denied += 1
        reason = (
            f"{name} is not available: Argus is a read-only reviewer "
            "and never changes the repository."
        )
        get_logger().warning("tool_denied", tool=name, input=_summarize(data.get("tool_input")))
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return hook


def audit_tool_call(state: HookState) -> Hook:
    async def hook(data: dict[str, Any], _tool_use_id: str | None, _ctx: HookContext) -> dict:
        state.tool_calls += 1
        get_logger().info(
            "tool_call", tool=data.get("tool_name"), input=_summarize(data.get("tool_input"))
        )
        return {}

    return hook


def audit_tool_failure(state: HookState) -> Hook:
    async def hook(data: dict[str, Any], _tool_use_id: str | None, _ctx: HookContext) -> dict:
        state.tool_failures += 1
        if data.get("tool_name") == STRUCTURED_OUTPUT_TOOL:
            state.output_rejections += 1
        get_logger().warning(
            "tool_failed", tool=data.get("tool_name"), error=_truncate(str(data.get("error", "")))
        )
        return {}

    return hook


def on_subagent_start(state: HookState) -> Hook:
    async def hook(data: dict[str, Any], _tool_use_id: str | None, _ctx: HookContext) -> dict:
        state.subagents_started += 1
        get_logger().info("subagent_start", agent=data.get("agent_type"), id=data.get("agent_id"))
        return {}

    return hook


def on_subagent_stop(state: HookState) -> Hook:
    async def hook(data: dict[str, Any], _tool_use_id: str | None, _ctx: HookContext) -> dict:
        state.subagents_stopped += 1
        get_logger().info("subagent_stop", agent=data.get("agent_type"), id=data.get("agent_id"))
        return {}

    return hook


def build_hooks(state: HookState) -> dict[HookEvent, list[HookMatcher]]:
    return {
        "PreToolUse": [
            HookMatcher(matcher=DENIED_TOOL_MATCHER, hooks=[deny_mutating_tools(state)])
        ],
        "PostToolUse": [HookMatcher(hooks=[audit_tool_call(state)])],
        "PostToolUseFailure": [HookMatcher(hooks=[audit_tool_failure(state)])],
        "SubagentStart": [HookMatcher(hooks=[on_subagent_start(state)])],
        "SubagentStop": [HookMatcher(hooks=[on_subagent_stop(state)])],
    }


def _summarize(tool_input: Any) -> str:
    try:
        text = json.dumps(tool_input, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(tool_input)
    return _truncate(text)


def _truncate(text: str) -> str:
    if len(text) <= _INPUT_SUMMARY_CHARS:
        return text
    return text[: _INPUT_SUMMARY_CHARS - 3] + "..."
