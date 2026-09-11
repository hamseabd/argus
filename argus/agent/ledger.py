"""Attributes one query's turns, tokens, and tool use to the lead and each subagent.

The SDK reports usage for the query as a whole. Attribution joins two
sources: every AssistantMessage names the Agent call that spawned its author
(None for the lead), and the lead's own Agent calls say which subagent type
each of those ids stands for; the tool hooks count calls and time per
subagent id. The result is one AgentMetrics per agent type, lead first.
"""

from dataclasses import dataclass, field

from claude_agent_sdk import AssistantMessage, ToolUseBlock

from argus.agent.hooks import LEAD_AGENT, AgentCounters, HookState
from argus.domain.models import AgentMetrics

AGENT_TOOL = "Agent"
_UNKNOWN_AGENT = "agent"


@dataclass
class _Tally:
    turns: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    seen: set[str] = field(default_factory=set)
    """API message ids already counted; the stream repeats one per content block."""


class AgentLedger:
    def __init__(self) -> None:
        self._spawned: dict[str, str] = {}  # Agent tool_use id -> subagent type
        self._tallies: dict[str, _Tally] = {}  # by agent name, in order of appearance

    def record(self, message: AssistantMessage) -> None:
        if message.parent_tool_use_id is None:
            self._note_delegations(message)
        tally = self._tallies.setdefault(self._name(message.parent_tool_use_id), _Tally())
        if message.message_id is not None:
            if message.message_id in tally.seen:
                return
            tally.seen.add(message.message_id)
        usage = message.usage or {}
        tally.turns += 1
        tally.output_tokens += usage.get("output_tokens") or 0
        tally.cache_read_input_tokens += usage.get("cache_read_input_tokens") or 0
        tally.cache_creation_input_tokens += usage.get("cache_creation_input_tokens") or 0

    def metrics(self, state: HookState) -> list[AgentMetrics]:
        counters = _counters_by_name(state)
        names = list(self._tallies) + [name for name in counters if name not in self._tallies]
        metrics = []
        for name in names:
            tally = self._tallies.get(name, _Tally())
            seen = counters.get(name, AgentCounters())
            metrics.append(
                AgentMetrics(
                    agent=name,
                    turns=tally.turns,
                    tool_calls=seen.tool_calls,
                    tool_failures=seen.tool_failures,
                    output_tokens=tally.output_tokens,
                    cache_read_input_tokens=tally.cache_read_input_tokens,
                    cache_creation_input_tokens=tally.cache_creation_input_tokens,
                    duration_ms=seen.duration_ms,
                )
            )
        return metrics

    def _note_delegations(self, message: AssistantMessage) -> None:
        self._tallies.setdefault(LEAD_AGENT, _Tally())
        for block in message.content:
            if isinstance(block, ToolUseBlock) and block.name == AGENT_TOOL:
                name = str(block.input.get("subagent_type") or _UNKNOWN_AGENT)
                self._spawned[block.id] = name
                self._tallies.setdefault(name, _Tally())

    def _name(self, parent_tool_use_id: str | None) -> str:
        if parent_tool_use_id is None:
            return LEAD_AGENT
        return self._spawned.get(parent_tool_use_id, _UNKNOWN_AGENT)


def _counters_by_name(state: HookState) -> dict[str, AgentCounters]:
    """Hook counters are per agent id; fold them by agent type, the lead under its own name."""
    folded: dict[str, AgentCounters] = {}
    for agent_id, counters in state.agents.items():
        if agent_id == LEAD_AGENT:
            name = LEAD_AGENT
        else:
            name = state.agent_types.get(agent_id, _UNKNOWN_AGENT)
        total = folded.setdefault(name, AgentCounters())
        total.tool_calls += counters.tool_calls
        total.tool_failures += counters.tool_failures
        total.duration_ms += counters.duration_ms
    return folded
