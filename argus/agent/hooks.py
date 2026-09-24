"""Hooks that keep the agent read-only and leave an audit trail.

allowed_tools already limits what the model can call. The PreToolUse hook
is defense in depth: if a mutating tool is ever requested it is denied with
a reason the model can read, so it does not retry. The other hooks only
observe: they log every tool call, count subagent starts so the pipeline
can tell whether the lead delegated as instructed, count rejected
structured outputs so a review that only validated after several attempts
is visible in the metrics, and keep per-agent tallies, since inside a
subagent the tool hooks carry that subagent's id and type.

When a TraceRecorder is attached, the same hooks feed it the start, end,
and denial of every tool call.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import HookContext, HookMatcher
from claude_agent_sdk.types import HookEvent

from argus.agent.tools import GIT_HISTORY_TOOL_NAME
from argus.telemetry import get_logger
from argus.tracing import redact, summarize

if TYPE_CHECKING:
    from argus.agent.trace import TraceRecorder

DENIED_TOOLS = frozenset(
    {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "WebFetch", "WebSearch"}
)
DENIED_TOOL_MATCHER = "|".join(sorted(DENIED_TOOLS))
ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob", "Agent", GIT_HISTORY_TOOL_NAME)
READ_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob", GIT_HISTORY_TOOL_NAME)
"""The tools that only look at code; the lead's reading budget governs these."""
READ_TOOL_MATCHER = "|".join(sorted(READ_TOOLS))
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
"""The SDK's internal tool that validates the final answer against the output schema.

The name comes from the bundled CLI, not the Python package, so nothing here
can pin it. If it ever changes the count silently reads zero; the schema's
length floor, not this counter, is what keeps a placeholder out of a review.
"""

LEAD_AGENT = "lead"
"""Key for the main thread; hook inputs from it carry no agent_id."""

_INPUT_SUMMARY_CHARS = 200

Hook = Callable[[dict[str, Any], str | None, HookContext], Awaitable[dict[str, Any]]]


@dataclass
class AgentCounters:
    """What the hooks can see of one agent: its tool use and, for subagents, its lifetime."""

    tool_calls: int = 0
    tool_failures: int = 0
    duration_ms: int = 0
    started_at: float | None = None


@dataclass
class HookState:
    """Counters one query's hooks update; read by the runner for metrics."""

    denied: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    output_rejections: int = 0
    subagents_started: int = 0
    subagents_stopped: int = 0
    agents: dict[str, AgentCounters] = field(default_factory=dict)
    """By agent_id, with LEAD_AGENT for the main thread."""
    agent_types: dict[str, str] = field(default_factory=dict)
    """agent_id to agent type, as the hooks reported it."""
    read_budget: int | None = None
    """Reads the main thread may make before it must answer; None means no limit."""
    lead_reads: int = 0
    reads_denied: int = 0
    clock: Callable[[], float] = time.monotonic
    recorder: "TraceRecorder | None" = None
    """Builds the query's spans; attached by the runner for the length of one query."""

    def agent(self, data: dict[str, Any]) -> AgentCounters:
        """The counters for whichever agent a hook input came from."""
        agent_id = data.get("agent_id")
        if agent_id and data.get("agent_type"):
            self.agent_types[agent_id] = str(data["agent_type"])
        return self.agents.setdefault(agent_id or LEAD_AGENT, AgentCounters())


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
        if state.recorder is not None:
            state.recorder.on_tool_denied(data, reason)
        get_logger().warning("tool_denied", tool=name, input=summarize(data.get("tool_input")))
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return hook


def limit_lead_reading(state: HookState) -> Hook:
    """Stop the lead reviewer from re-reading the change it has delegated.

    The lead delegates and merges; the specialists read, and the verifier
    reads again for every finding. Measured runs still showed the lead
    spending forty tool calls on a sweep of its own, so the budget is
    enforced here instead of asked for in the prompt. Only reading is
    refused: delegation and the final answer always go through.
    """

    async def hook(data: dict[str, Any], _tool_use_id: str | None, _ctx: HookContext) -> dict:
        budget = state.read_budget
        if budget is None or data.get("agent_id") or data.get("tool_name") not in READ_TOOLS:
            return {}
        if state.lead_reads < budget:
            state.lead_reads += 1
            return {}
        state.reads_denied += 1
        reason = (
            f"You have used the {budget} reads a lead gets. The specialists have read "
            "the change for you: merge what they reported and answer with the Review."
        )
        get_logger().warning("lead_read_denied", tool=data.get("tool_name"), budget=budget)
        if state.recorder is not None:
            state.recorder.on_tool_denied(data, reason)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return hook


def observe_tool_start(state: HookState) -> Hook:
    async def hook(data: dict[str, Any], _tool_use_id: str | None, _ctx: HookContext) -> dict:
        if state.recorder is not None:
            state.recorder.on_tool_start(data)
        return {}

    return hook


def audit_tool_call(state: HookState) -> Hook:
    async def hook(data: dict[str, Any], _tool_use_id: str | None, _ctx: HookContext) -> dict:
        state.tool_calls += 1
        state.agent(data).tool_calls += 1
        get_logger().info(
            "tool_call", tool=data.get("tool_name"), input=summarize(data.get("tool_input"))
        )
        if state.recorder is not None:
            state.recorder.on_tool_end(data)
        return {}

    return hook


def audit_tool_failure(state: HookState) -> Hook:
    async def hook(data: dict[str, Any], _tool_use_id: str | None, _ctx: HookContext) -> dict:
        state.tool_failures += 1
        state.agent(data).tool_failures += 1
        if data.get("tool_name") == STRUCTURED_OUTPUT_TOOL:
            state.output_rejections += 1
        get_logger().warning(
            "tool_failed", tool=data.get("tool_name"), error=_truncate(str(data.get("error", "")))
        )
        if state.recorder is not None:
            state.recorder.on_tool_end(data, error=str(data.get("error", "")))
        return {}

    return hook


def on_subagent_start(state: HookState) -> Hook:
    async def hook(data: dict[str, Any], _tool_use_id: str | None, _ctx: HookContext) -> dict:
        state.subagents_started += 1
        state.agent(data).started_at = state.clock()
        get_logger().info("subagent_start", agent=data.get("agent_type"), id=data.get("agent_id"))
        if state.recorder is not None:
            state.recorder.on_subagent_start(data)
        return {}

    return hook


def on_subagent_stop(state: HookState) -> Hook:
    async def hook(data: dict[str, Any], _tool_use_id: str | None, _ctx: HookContext) -> dict:
        state.subagents_stopped += 1
        counters = state.agent(data)
        if counters.started_at is not None:
            counters.duration_ms = round((state.clock() - counters.started_at) * 1000)
        get_logger().info(
            "subagent_stop",
            agent=data.get("agent_type"),
            id=data.get("agent_id"),
            duration_ms=counters.duration_ms,
            tool_calls=counters.tool_calls,
        )
        return {}

    return hook


def build_hooks(state: HookState) -> dict[HookEvent, list[HookMatcher]]:
    return {
        "PreToolUse": [
            HookMatcher(matcher=DENIED_TOOL_MATCHER, hooks=[deny_mutating_tools(state)]),
            HookMatcher(matcher=READ_TOOL_MATCHER, hooks=[limit_lead_reading(state)]),
            HookMatcher(hooks=[observe_tool_start(state)]),
        ],
        "PostToolUse": [HookMatcher(hooks=[audit_tool_call(state)])],
        "PostToolUseFailure": [HookMatcher(hooks=[audit_tool_failure(state)])],
        "SubagentStart": [HookMatcher(hooks=[on_subagent_start(state)])],
        "SubagentStop": [HookMatcher(hooks=[on_subagent_stop(state)])],
    }


def _truncate(text: str) -> str:
    return redact(text, _INPUT_SUMMARY_CHARS)
