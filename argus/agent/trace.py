"""Builds one query's spans from its message stream and its hooks.

The SDK reports a query's usage as a whole and its native spans are not
exported (see argus.tracing), so the tree is rebuilt here from what the
harness already sees:

- The lead's Agent tool call is the subagent's span, keyed by its
  tool_use_id and named after tool_input["subagent_type"].
- A subagent's AssistantMessages name that tool_use_id as their parent.
- A subagent's tool hooks carry its agent_id instead; SubagentStart gives
  agent_id and type, and claims the oldest unclaimed Agent call of that
  type. Specialist types are distinct, so the claim is unambiguous.

Hooks for one tool call may arrive in any order (matching hooks run
concurrently), so every entry point creates the span if it is missing and
a tool_use_id, once ended, is never reopened. AssistantMessage carries no
timestamps: an llm span starts at its agent's previous event and ends on
arrival, and says so in its attributes.

The recorder starts spans with tracer().start_span(...) directly rather
than through argus.tracing.span(), so it is not covered by that helper's
own redaction; every attribute dict built here passes through clean()
before it reaches start_span or set_attributes.
"""

import json
import time
from collections.abc import Callable
from typing import Any

from claude_agent_sdk import AssistantMessage, TextBlock
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from argus.tracing import ERROR_CHARS, KIND, clean, meta, redact, tracer

AGENT_TOOL = "Agent"
_INPUT_CHARS = 200
_UNFINISHED = "the query ended before this call finished"


class TraceRecorder:
    def __init__(self, *, content: bool = False, clock: Callable[[], int] = time.time_ns) -> None:
        self._root = trace.set_span_in_context(trace.get_current_span())
        self._content = content
        self._clock = clock
        self._spans: dict[str, Span] = {}  # tool_use_id -> span, kept after it ends for parenting
        self._open: set[str] = set()
        self._ended: set[str] = set()
        self._unclaimed: dict[str, list[str]] = {}  # subagent type -> Agent tool_use_ids
        self._agents: dict[str, str] = {}  # subagent agent_id -> its Agent tool_use_id
        self._last: dict[str | None, int] = {None: clock()}  # agent key -> time of its last event
        self._seen: set[str] = set()

    def on_assistant(self, message: AssistantMessage) -> None:
        if message.message_id is not None:
            if message.message_id in self._seen:
                return
            self._seen.add(message.message_id)
        key = message.parent_tool_use_id
        now = self._clock()
        usage = message.usage or {}
        uncached = usage.get("input_tokens") or 0
        cache_read = usage.get("cache_read_input_tokens") or 0
        cache_creation = usage.get("cache_creation_input_tokens") or 0
        output = usage.get("output_tokens") or 0
        total_input = uncached + cache_read + cache_creation
        attrs: dict[str, AttributeValue] = {
            KIND: "llm",
            "gen_ai.system": "anthropic",
            "gen_ai.request.model": message.model,
            "gen_ai.usage.input_tokens": total_input,
            "gen_ai.usage.output_tokens": output,
            "gen_ai.usage.total_tokens": total_input + output,
            **meta(
                uncached_input_tokens=uncached,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_creation,
                stop_reason=message.stop_reason,
                timing="approximate",
            ),
        }
        if self._content:
            text = "\n".join(b.text for b in message.content if isinstance(b, TextBlock))
            if text:
                attrs["output.value"] = redact(text)
        start = self._last.get(key, now)
        llm = tracer().start_span(
            "llm", context=self._parent(key), attributes=clean(attrs), start_time=start
        )
        llm.end(end_time=now)
        self._last[key] = now

    def on_tool_start(self, data: dict[str, Any]) -> None:
        tool_use_id = data.get("tool_use_id")
        if not tool_use_id or tool_use_id in self._spans:
            return
        name = str(data.get("tool_name") or "tool")
        now = self._clock()
        attrs: dict[str, AttributeValue] = {
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.id": tool_use_id,
        }
        if name == AGENT_TOOL:
            subagent = _subagent_type(data.get("tool_input"))
            span_name, attrs[KIND] = subagent, "chain"
            attrs |= meta(agent=subagent)
            self._unclaimed.setdefault(subagent, []).append(tool_use_id)
            self._last[tool_use_id] = now
        else:
            span_name, attrs[KIND] = name, "tool"
            attrs["input.value"] = _summary(data.get("tool_input"))
        owner = self._owner(data)
        self._spans[tool_use_id] = tracer().start_span(
            span_name, context=self._parent(owner), attributes=clean(attrs), start_time=now
        )
        self._open.add(tool_use_id)

    def on_tool_end(self, data: dict[str, Any], error: str | None = None) -> None:
        tool_use_id = data.get("tool_use_id")
        if tool_use_id not in self._open:
            return
        span = self._spans[tool_use_id]
        if error is not None:
            span.set_status(Status(StatusCode.ERROR, redact(error, ERROR_CHARS)))
        self._finish(tool_use_id, self._owner(data))

    def on_tool_denied(self, data: dict[str, Any], reason: str) -> None:
        tool_use_id = data.get("tool_use_id")
        if not tool_use_id or tool_use_id in self._ended:
            return
        self.on_tool_start(data)
        self._spans[tool_use_id].set_attributes(
            clean(meta(denied=True, denial=redact(reason, ERROR_CHARS)))
        )
        self._finish(tool_use_id, self._owner(data))

    def on_subagent_start(self, data: dict[str, Any]) -> None:
        agent_id, agent_type = data.get("agent_id"), data.get("agent_type")
        queue = self._unclaimed.get(str(agent_type), [])
        if not agent_id or not queue:
            return
        tool_use_id = queue.pop(0)
        self._agents[agent_id] = tool_use_id
        self._spans[tool_use_id].set_attributes(clean(meta(agent_id=agent_id)))

    def close(self) -> None:
        """End whatever the query left open, as errors; safe to call twice."""
        for tool_use_id in list(self._open):
            self._spans[tool_use_id].set_status(Status(StatusCode.ERROR, _UNFINISHED))
            self._finish(tool_use_id, None)

    def _finish(self, tool_use_id: str, owner: str | None) -> None:
        now = self._clock()
        self._spans[tool_use_id].end(end_time=now)
        self._open.discard(tool_use_id)
        self._ended.add(tool_use_id)
        self._last[owner] = now

    def _owner(self, data: dict[str, Any]) -> str | None:
        """The Agent tool_use_id of the subagent a hook came from; None for the lead."""
        agent_id = data.get("agent_id")
        return self._agents.get(agent_id) if agent_id else None

    def _parent(self, key: str | None) -> Any:
        span = self._spans.get(key) if key else None
        return trace.set_span_in_context(span) if span is not None else self._root


def _subagent_type(tool_input: Any) -> str:
    requested = tool_input.get("subagent_type") if isinstance(tool_input, dict) else None
    return requested if isinstance(requested, str) and requested else "agent"


def _summary(tool_input: Any) -> str:
    try:
        text = json.dumps(tool_input, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(tool_input)
    return redact(text, _INPUT_CHARS)
