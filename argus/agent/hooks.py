"""Hooks that keep the agent read-only and leave an audit trail.

allowed_tools already limits what the model can call. The PreToolUse hook
is defense in depth: if a mutating tool is ever requested it is denied with
a reason the model can read, so it does not retry. Other PreToolUse hooks
keep reads inside the repository, cap the lead's own reads, hold the lead's
answer until every specialist has reported, and deny a report_finding call
the tool would refuse, saying what to fix, closing the tool to a reviewer
stuck repeating one mistake. The rest only observe: they log every tool
call, count subagent starts so the pipeline can tell whether the lead
delegated as instructed, count rejected structured outputs so a review that
only validated after several attempts is visible in the metrics, keep the
lead's last accepted answer so a run whose final turn ends in plain text can
still return it, and keep per-agent tallies, since inside a subagent the
tool hooks carry that subagent's id and type.

When a TraceRecorder is attached, the same hooks feed it the start, end,
and denial of every tool call.
"""

import fnmatch
import os
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from claude_agent_sdk import HookContext, HookMatcher
from claude_agent_sdk.types import HookCallback, HookEvent

from argus.agent.findings import RECORDED_ID, FindingCollector
from argus.agent.tools import GIT_HISTORY_TOOL_NAME, REPORT_FINDING_TOOL_NAME
from argus.context.source import within_repository
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

REPORT_REFUSAL_LIMIT = 3
"""Identical report_finding refusals in a row after which the tool is closed to that agent."""
_REPORTS_CLOSED = (
    "report_finding is closed to you for the rest of this review: stop calling it "
    "and finish with the findings already recorded."
)

_INPUT_SUMMARY_CHARS = 200
_GREP_GLOB_SEPARATORS = re.compile(r"[\s,]+")
"""Grep's glob filter holds several globs, split on whitespace and commas."""

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
    report_refusals: dict[str, tuple[str, int]] = field(default_factory=dict)
    """By agent_id: the reason its last report_finding call was refused, and how many in a row."""
    reports_closed: set[str] = field(default_factory=set)
    """Agents whose identical refusals reached REPORT_REFUSAL_LIMIT; report_finding is refused."""
    reports_refused: int = 0
    """report_finding calls check_reports refused, closed agents' calls included."""

    def agent(self, data: HookData) -> AgentCounters:
        """The counters for whichever agent a hook input came from."""
        agent_id = data.get("agent_id")
        if agent_id and data.get("agent_type"):
            self.agent_types[agent_id] = str(data["agent_type"])
        return self.agents.setdefault(agent_id or LEAD_AGENT, AgentCounters())

    def reporter_type(self, agent_id: str) -> str:
        """The type of the agent behind agent_id, as the hooks reported it; the lead is "lead"."""
        return self.agent_types.get(agent_id, agent_id)

    def closed_reporters(self) -> list[str]:
        """The types of the agents report_finding was closed to, sorted."""
        return sorted(self.reporter_type(agent) for agent in self.reports_closed)


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

    A search reaches files by pattern, not by a path that can be resolved, so
    the repository is scanned once for links that lead out of it or into .git,
    and a Grep or Glob whose scope holds one is refused whatever its pattern.
    """
    root = repo_root.resolve()
    links = escaping_links(root)

    async def hook(data: HookData, _tool_use_id: str | None, _ctx: HookContext) -> dict[str, Any]:
        name = data.get("tool_name", "")
        if name not in PATH_TOOLS:
            return {}
        problem = _outside(root, name, data.get("tool_input") or {}, links)
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


def _outside(
    root: Path, tool: str, tool_input: dict[str, Any], links: Sequence[Path] = ()
) -> str | None:
    """The offending path of a Read, Grep, or Glob call, or None when it stays inside root."""
    if tool == "Read":
        raw = tool_input.get("file_path")
        if not raw:
            return "a read with no file_path"
        return None if _inside(root, str(raw)) else str(raw)
    path = str(tool_input.get("path") or ".")
    if not _inside(root, path):
        return path
    scope = (root / Path(path).expanduser()).resolve()
    for link in links:
        if (root / link).is_relative_to(scope):
            return f"{path} (it holds {link.as_posix()}, a link that leads out of it)"
    # A glob can climb out or into .git on its own, whatever path it starts from:
    # Glob's pattern and each of the globs in Grep's glob filter are checked as patterns.
    if tool == "Glob":
        patterns = [str(tool_input.get("pattern") or "")]
    else:
        patterns = _GREP_GLOB_SEPARATORS.split(str(tool_input.get("glob") or ""))
    return next((p for p in map(_escaping_pattern, filter(None, patterns)) if p), None)


def escaping_links(root: Path) -> list[Path]:
    """Symlinks in the repository that resolve outside it or into its .git, relative to root."""
    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        # Only the real .git is skipped: a .GIT that is a separate directory, on a
        # case-sensitive filesystem, is scanned like any other.
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in [*dirnames, *filenames]:
            entry = Path(directory) / name
            if entry.is_symlink() and not _inside(root, str(entry)):
                found.append(entry.relative_to(root))
    return found


def _escaping_pattern(pattern: str) -> str | None:
    """Why a pattern can name a path outside the repository or in its .git, or None.

    The rule set is the part every glob dialect shares: `*`, `?`, and literal
    text, with `/` between segments. Character classes, backslash escapes, and
    brace sets are where the search tools' dialect (ripgrep's) and fnmatch part
    ways, as `[^a]`, a `/` inside a class, or `{..,x}` do, so a pattern holding
    `[`, `\\`, or `{` is refused whatever it says. What is left is checked
    exactly: an absolute or home pattern is refused, and so is a segment that
    is `..` or that could match `.git` in any case, such as `.gi?` or `.G*`.

    A bare `*` or `**` segment is allowed. This check refuses a pattern that
    names a parent or .git; it does not stop a wildcard that happens to match
    .git, since refusing those would refuse nearly every search. That is left
    to the search tools, which skip hidden directories, and to the checkout,
    which keeps no credential in .git.
    """
    if any(symbol in pattern for symbol in "[\\{"):
        return f"{pattern} (character classes, escapes, and brace sets are refused)"
    if pattern.startswith(("/", "~")):
        return pattern
    for segment in pattern.split("/"):
        if segment == ".." or (
            segment not in ("*", "**") and fnmatch.fnmatchcase(".git", segment.casefold())
        ):
            return pattern
    return None


def _inside(root: Path, raw: str) -> bool:
    try:
        path = Path(raw).expanduser()
        resolved = (path if path.is_absolute() else root / path).resolve()
    except (RuntimeError, OSError):  # an unknown ~user, a symlink loop: refuse, never guess
        return False
    return within_repository(root, resolved)


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
                "then answer with the Triage."
            )
            log.warning("answer_held", waiting=waiting)
        else:
            finished = sorted(set(state.finished))
            reason = (
                f"{', '.join(finished)} just finished; its report may not have reached you "
                "yet. Wait for it, then answer with the Triage."
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
            "the change for you and recorded what they found: answer with the Triage."
        )
        get_logger().warning("lead_read_denied", tool=data.get("tool_name"), budget=budget)
        return _deny(state, data, reason)

    return hook


def check_reports(state: HookState) -> Hook:
    """Refuse a report_finding call the tool would refuse, saying what to fix, and end a loop.

    The SDK validates the call against the schema before the tool runs and
    answers a missing field with its name alone, the same answer every time, so a
    reviewer that keeps leaving evidence out can spend turns until the budget
    runs out. This answers first, naming each missing field and what it must
    hold. A reviewer refused for the same reason REPORT_REFUSAL_LIMIT times in a
    row has report_finding closed for the rest of the review: each refusal still
    costs it a turn, but it is told to stop, and its maxTurns bound the rest.
    """

    async def hook(data: HookData, _tool_use_id: str | None, _ctx: HookContext) -> dict[str, Any]:
        if state.collector is None:
            return {}
        agent = data.get("agent_id") or LEAD_AGENT
        state.agent(data)  # records the agent's type, which the logs and the review name
        reporter = state.reporter_type(agent)
        log = get_logger()
        if agent in state.reports_closed:
            state.reports_refused += 1
            return _deny(state, data, _REPORTS_CLOSED)
        problem = state.collector.problem(data.get("tool_input") or {})
        if problem is None:
            state.report_refusals.pop(agent, None)
            return {}
        state.reports_refused += 1
        last, count = state.report_refusals.get(agent, ("", 0))
        count = count + 1 if problem == last else 1
        state.report_refusals[agent] = (problem, count)
        log.warning(
            "report_refused",
            agent=agent,
            agent_type=reporter,
            repeats=count,
            reason=_truncate(problem),
        )
        reason = f"Not recorded: {problem}"
        if count >= REPORT_REFUSAL_LIMIT:
            state.reports_closed.add(agent)
            log.warning("report_finding_closed", agent=agent, agent_type=reporter, repeats=count)
            reason += f" That is {count} identical refusals in a row. {_REPORTS_CLOSED}"
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
            _matcher([check_reports(state)], REPORT_FINDING_TOOL_NAME),
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
