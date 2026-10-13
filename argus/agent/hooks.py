"""Hooks that keep the agent read-only and leave an audit trail.

allowed_tools already limits what the model can call. The PreToolUse hook
is defense in depth: if a mutating tool is ever requested it is denied with
a reason the model can read, so it does not retry. The other hooks only
observe: they log every tool call, count subagent starts so the pipeline
can tell whether the lead delegated as instructed, count rejected
structured outputs so a review that only validated after several attempts
is visible in the metrics, keep the lead's last accepted answer so a run
whose final turn ends in plain text can still return it, and keep per-agent tallies, since inside a
subagent the tool hooks carry that subagent's id and type.

When a TraceRecorder is attached, the same hooks feed it the start, end,
and denial of every tool call.
"""

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from claude_agent_sdk import HookContext, HookMatcher
from claude_agent_sdk.types import HookCallback, HookEvent

from argus.agent.findings import RECORDED_ID, FindingCollector
from argus.agent.tools import GIT_HISTORY_TOOL_NAME, REPORT_FINDING_TOOL_NAME
from argus.settings import DEFAULT_ANSWER_HOLDS
from argus.telemetry import get_logger
from argus.tracing import redact, summarize

if TYPE_CHECKING:
    from argus.agent.trace import TraceRecorder

DENIED_TOOLS = frozenset(
    {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "WebFetch", "WebSearch"}
)
DENIED_TOOL_MATCHER = "|".join(sorted(DENIED_TOOLS))
ALLOWED_TOOLS: tuple[str, ...] = (
    "Read",
    "Grep",
    "Glob",
    "Agent",
    GIT_HISTORY_TOOL_NAME,
    REPORT_FINDING_TOOL_NAME,
)
READ_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob", GIT_HISTORY_TOOL_NAME)
"""The tools that only look at code; the lead's reading budget governs these."""
READ_TOOL_MATCHER = "|".join(sorted(READ_TOOLS))
PATH_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")
"""The built-in tools that take a path; confine_reads keeps them inside the repository.

git_history checks its own path, in argus.agent.tools.
"""
PATH_TOOL_MATCHER = "|".join(PATH_TOOLS)
AGENT_TOOL = "Agent"
"""The tool the lead delegates with; its calls are what the ledger and the trace attribute to."""
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
"""The SDK's internal tool that validates the final answer against the output schema.

The name comes from the bundled CLI, not the Python package, so nothing here
can pin it. If it ever changes the count silently reads zero; the schema's
length floor, not this counter, is what keeps a placeholder out of a review.
"""

LEAD_AGENT = "lead"
"""Key for the main thread; hook inputs from it carry no agent_id."""

_INPUT_SUMMARY_CHARS = 200

HookData = dict[str, Any]
"""A hook's input as Argus reads it: a plain mapping; build_hooks casts the SDK's typed unions."""
Hook = Callable[[HookData, str | None, HookContext], Awaitable[dict[str, Any]]]


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
    running: dict[str, str] = field(default_factory=dict)
    """Subagents started and not yet stopped: agent_id to agent type."""
    finished: list[str] = field(default_factory=list)
    """Subagent types that stopped since the lead's answer was last held."""
    answers_held: int = 0
    answer_hold_limit: int = DEFAULT_ANSWER_HOLDS
    """Holds after which the answer goes through regardless, so a hold cannot last forever."""
    accepted_answer: Any = None
    """The lead's last structured output the SDK accepted; the runner falls back to it."""
    clock: Callable[[], float] = time.monotonic
    recorder: "TraceRecorder | None" = None
    """Builds the query's spans; attached by the runner for the length of one query."""
    collector: FindingCollector | None = None
    """Where report_finding records findings; the hooks attribute each id to its reporter."""

    def agent(self, data: HookData) -> AgentCounters:
        """The counters for whichever agent a hook input came from."""
        agent_id = data.get("agent_id")
        if agent_id and data.get("agent_type"):
            self.agent_types[agent_id] = str(data["agent_type"])
        return self.agents.setdefault(agent_id or LEAD_AGENT, AgentCounters())


def deny_mutating_tools(state: HookState) -> Hook:
    async def hook(data: HookData, _tool_use_id: str | None, _ctx: HookContext) -> dict[str, Any]:
        name = data.get("tool_name", "")
        if name not in DENIED_TOOLS:
            return {}
        state.denied += 1
        reason = (
            f"{name} is not available: Argus is a read-only reviewer "
            "and never changes the repository."
        )
        get_logger().warning("tool_denied", tool=name, input=summarize(data.get("tool_input")))
        return _deny(state, data, reason)

    return hook


def confine_reads(state: HookState, repo_root: Path) -> Hook:
    """Refuse a read that would leave the repository under review or enter its .git.

    The tool set is read-only, but a read is still an exfiltration path: a
    prompt injection in the diff, the PR body, or a symlink the change adds
    can ask for /proc/self/environ or the CLI's credentials, and what a
    reviewer reads can be quoted in a finding that is posted on the pull
    request. Paths are resolved with symlinks followed, so a link inside the
    repository that points out of it is refused too. .git is refused because
    a checkout can keep a token in its config.
    """
    root = repo_root.resolve()

    async def hook(data: HookData, _tool_use_id: str | None, _ctx: HookContext) -> dict[str, Any]:
        name = data.get("tool_name", "")
        if name not in PATH_TOOLS:
            return {}
        problem = _outside(root, name, data.get("tool_input") or {})
        if problem is None:
            return {}
        state.denied += 1
        get_logger().warning(
            "read_outside_repo", tool=name, input=summarize(data.get("tool_input"))
        )
        reason = (
            f"{problem} is outside the repository under review. Argus reads only the "
            "repository's own files, and never its .git directory."
        )
        return _deny(state, data, reason)

    return hook


def _outside(root: Path, tool: str, tool_input: dict[str, Any]) -> str | None:
    """The offending path of a Read, Grep, or Glob call, or None when it stays inside root."""
    if tool == "Read":
        raw = tool_input.get("file_path")
        if not raw:
            return "a read with no file_path"
        return None if _inside(root, str(raw)) else str(raw)
    path = str(tool_input.get("path") or ".")
    if not _inside(root, path):
        return path
    # A glob can climb out or into .git on its own, whatever path it starts from:
    # Glob's pattern and Grep's glob filter are checked as patterns.
    pattern_key = "pattern" if tool == "Glob" else "glob"
    pattern = str(tool_input.get(pattern_key) or "")
    if pattern and _escaping_pattern(pattern):
        return pattern
    return None


def _escaping_pattern(pattern: str) -> bool:
    """An absolute, home, parent, .git, or brace pattern: any of them can leave the repository.

    Braces are refused outright because `{..,x}` expands to a parent directory
    that a check of the literal parts cannot see.
    """
    if pattern.startswith(("/", "~")) or "{" in pattern:
        return True
    parts = pattern.replace("\\", "/").split("/")
    return ".." in parts or ".git" in parts


def _inside(root: Path, raw: str) -> bool:
    try:
        path = Path(raw).expanduser()
        resolved = (path if path.is_absolute() else root / path).resolve()
    except (RuntimeError, OSError):  # an unknown ~user, a symlink loop: refuse, never guess
        return False
    if not resolved.is_relative_to(root):
        return False
    return ".git" not in resolved.relative_to(root).parts


def await_specialists(state: HookState) -> Hook:
    """Hold the lead's answer until every specialist it started has reported.

    The CLI backgrounds Agent calls by default, and then the lead's Agent call
    returns before the specialist has reported. The options turn background
    tasks off, so this is the second line of defense. An answer submitted then
    misses that specialist's findings, and its late report starts more turns
    that end in plain text, which fails the run. So the answer is refused,
    naming who is still running, until the last one stops, and then once
    more: SubagentStop fires before the report reaches the lead, so an answer
    right after it can still predate the findings. Holds are bounded by
    answer_hold_limit; past it the answer goes through, and the runner's
    recovery of the accepted answer covers what follows.
    """

    async def hook(data: HookData, _tool_use_id: str | None, _ctx: HookContext) -> dict[str, Any]:
        if data.get("agent_id"):
            return {}
        log = get_logger()
        if not state.running and not state.finished:
            if state.answers_held:
                log.info("answer_hold_released", held=state.answers_held, reason="reported")
            return {}
        if state.answers_held >= state.answer_hold_limit:
            log.warning(
                "answer_hold_released",
                held=state.answers_held,
                reason="hold_limit",
                waiting=sorted(set(state.running.values())),
            )
            return {}
        if state.running:
            waiting = sorted(set(state.running.values()))
            reason = (
                f"Still running: {', '.join(waiting)}. Wait for their findings, "
                "merge them with the others, then answer with the Review."
            )
            log.warning("answer_held", waiting=waiting)
        else:
            finished = sorted(set(state.finished))
            reason = (
                f"{', '.join(finished)} just finished; its report may not have reached you "
                "yet. Wait for it, merge it with the others, then answer with the Review."
            )
            log.warning("answer_held", finished=finished)
        state.finished.clear()
        state.answers_held += 1
        return _deny(state, data, reason)

    return hook


def limit_lead_reading(state: HookState) -> Hook:
    """Stop the lead reviewer from re-reading the change it has delegated.

    The lead delegates and merges; the specialists read, and the verifier
    reads again for every finding. Measured runs still showed the lead
    spending forty tool calls on a sweep of its own, so the budget is
    enforced here instead of asked for in the prompt. Only reading is
    refused: delegation and the final answer always go through.
    """

    async def hook(data: HookData, _tool_use_id: str | None, _ctx: HookContext) -> dict[str, Any]:
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
        return _deny(state, data, reason)

    return hook


def _deny(state: HookState, data: HookData, reason: str) -> dict[str, Any]:
    """The PreToolUse output that refuses a call, with the reason the model will read.

    The refusal is recorded on the trace too, as a span of the call that never ran.
    """
    if state.recorder is not None:
        state.recorder.on_tool_denied(data, reason)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def observe_tool_start(state: HookState) -> Hook:
    async def hook(data: HookData, _tool_use_id: str | None, _ctx: HookContext) -> dict[str, Any]:
        if state.recorder is not None:
            state.recorder.on_tool_start(data)
        return {}

    return hook


def audit_tool_call(state: HookState) -> Hook:
    async def hook(data: HookData, _tool_use_id: str | None, _ctx: HookContext) -> dict[str, Any]:
        state.tool_calls += 1
        state.agent(data).tool_calls += 1
        if data.get("tool_name") == STRUCTURED_OUTPUT_TOOL and not data.get("agent_id"):
            # PostToolUse fires only once the SDK has validated the answer; a
            # rejected one goes to PostToolUseFailure instead.
            state.accepted_answer = data.get("tool_input")
        if data.get("tool_name") == REPORT_FINDING_TOOL_NAME:
            _attribute_report(state, data)
        get_logger().info(
            "tool_call", tool=data.get("tool_name"), input=summarize(data.get("tool_input"))
        )
        if state.recorder is not None:
            state.recorder.on_tool_end(data)
        return {}

    return hook


def _attribute_report(state: HookState, data: HookData) -> None:
    """Credit a recorded finding to whoever reported it; the tool's handler cannot see that."""
    if state.collector is None:
        return
    match = RECORDED_ID.search(str(data.get("tool_response", "")))
    if match is None:
        return
    agent_id = data.get("agent_id")
    if agent_id:
        reporter = str(data.get("agent_type") or state.agent_types.get(agent_id) or "agent")
    else:
        reporter = LEAD_AGENT
    state.collector.attribute(match.group(1), reporter)


def audit_tool_failure(state: HookState) -> Hook:
    async def hook(data: HookData, _tool_use_id: str | None, _ctx: HookContext) -> dict[str, Any]:
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
    async def hook(data: HookData, _tool_use_id: str | None, _ctx: HookContext) -> dict[str, Any]:
        state.subagents_started += 1
        state.agent(data).started_at = state.clock()
        if data.get("agent_id"):
            state.running[str(data["agent_id"])] = str(data.get("agent_type") or "agent")
        get_logger().info("subagent_start", agent=data.get("agent_type"), id=data.get("agent_id"))
        if state.recorder is not None:
            state.recorder.on_subagent_start(data)
        return {}

    return hook


def on_subagent_stop(state: HookState) -> Hook:
    async def hook(data: HookData, _tool_use_id: str | None, _ctx: HookContext) -> dict[str, Any]:
        state.subagents_stopped += 1
        if data.get("agent_id"):
            state.running.pop(str(data["agent_id"]), None)
            state.finished.append(str(data.get("agent_type") or "agent"))
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
        if state.recorder is not None:
            state.recorder.on_subagent_stop(data)
        return {}

    return hook


def build_hooks(state: HookState, repo_root: Path) -> dict[HookEvent, list[HookMatcher]]:
    return {
        "PreToolUse": [
            _matcher([deny_mutating_tools(state)], DENIED_TOOL_MATCHER),
            _matcher([limit_lead_reading(state)], READ_TOOL_MATCHER),
            _matcher([await_specialists(state)], STRUCTURED_OUTPUT_TOOL),
            _matcher([confine_reads(state, repo_root)], PATH_TOOL_MATCHER),
            _matcher([observe_tool_start(state)]),
        ],
        "PostToolUse": [_matcher([audit_tool_call(state)])],
        "PostToolUseFailure": [_matcher([audit_tool_failure(state)])],
        "SubagentStart": [_matcher([on_subagent_start(state)])],
        "SubagentStop": [_matcher([on_subagent_stop(state)])],
    }


def _matcher(hooks: Sequence[Hook], matcher: str | None = None) -> HookMatcher:
    """A HookMatcher over Argus hooks.

    The SDK types a hook's input as a union of per-event TypedDicts and its
    output as a union of result shapes. Argus hooks read the few keys they need
    from a plain mapping and return a plain dict, so the one cast lives here.
    """
    return HookMatcher(matcher=matcher, hooks=[cast(HookCallback, hook) for hook in hooks])


def _truncate(text: str) -> str:
    return redact(text, _INPUT_SUMMARY_CHARS)
