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
timestamps, and the stream repeats one message_id once per content block,
with the tool hooks for earlier blocks firing between copies: a turn's llm
span starts on the first copy, counting that copy's usage, then stays
pending, collecting text and the latest stop_reason across copies, until
a different message_id for the same agent, a message without one, or
close() flushes it, ending it at the last copy seen and saying so in its
attributes. A copy that arrives after its turn was flushed is dropped, so
no turn is counted twice.

The recorder starts spans with tracer().start_span(...) directly rather
than through argus.tracing.span(), so it is not covered by that helper's
own redaction; every attribute dict built here passes through clean()
before it reaches start_span or set_attributes.

Tracing must never change a review's outcome. Every public method is
wrapped with _never_raises: an internal bug is logged and swallowed
instead of propagating into a hook or the runner's message loop.
"""

import functools
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import AssistantMessage, TextBlock
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from argus.telemetry import get_logger
from argus.tracing import ERROR_CHARS, KIND, clean, meta, redact, tracer

AGENT_TOOL = "Agent"
_INPUT_CHARS = 200
_UNFINISHED = "the query ended before this call finished"


def _never_raises(fn: Callable[..., None]) -> Callable[..., None]:
    """Catch, log, and swallow: a tracing bug must never fail the review it is watching."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            get_logger().warning(
                "trace_record_failed",
                method=fn.__name__,
                error=redact(f"{type(exc).__name__}: {exc}", ERROR_CHARS),
            )
        return None

    return wrapper


@dataclass
class _PendingTurn:
    """An llm span still open across one message_id's repeated stream copies."""

    span: Span
    message_id: str | None = None
    texts: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    last: int = 0


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
        self._pending: dict[str | None, _PendingTurn] = {}  # agent key -> its open llm span
        self._started: set[str] = set()  # message_ids that already opened an llm span

    @_never_raises
    def on_assistant(self, message: AssistantMessage) -> None:
        key = message.parent_tool_use_id
        now = self._clock()
        mid = message.message_id
        pending = self._pending.get(key)
        if pending is None or mid is None or pending.message_id != mid:
            if mid is not None and mid in self._started:
                return  # a late copy of a turn already flushed; counted once already
            self._flush(key)
            self._start_llm(key, message, now)
            pending = self._pending[key]
            pending.message_id = mid
            if mid is not None:
                self._started.add(mid)
        pending.texts.extend(b.text for b in message.content if isinstance(b, TextBlock))
        if message.stop_reason is not None:
            pending.stop_reason = message.stop_reason
        pending.last = now
        if mid is None:  # not part of a stream; its own turn, start to finish
            self._flush(key)

    @_never_raises
    def on_tool_start(self, data: dict[str, Any]) -> None:
        tool_use_id = data.get("tool_use_id")
        if not tool_use_id or tool_use_id in self._spans:
            return
        owner = self._owner(data)
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
        self._spans[tool_use_id] = tracer().start_span(
            span_name, context=self._parent(owner), attributes=clean(attrs), start_time=now
        )
        self._open.add(tool_use_id)

    @_never_raises
    def on_tool_end(self, data: dict[str, Any], error: str | None = None) -> None:
        tool_use_id = data.get("tool_use_id")
        if tool_use_id not in self._open:
            return
        owner = self._owner(data)
        span = self._spans[tool_use_id]
        if error is not None:
            span.set_status(Status(StatusCode.ERROR, redact(error, ERROR_CHARS)))
        self._finish(tool_use_id, owner)

    @_never_raises
    def on_tool_denied(self, data: dict[str, Any], reason: str) -> None:
        tool_use_id = data.get("tool_use_id")
        if not tool_use_id or tool_use_id in self._ended:
            return
        owner = self._owner(data)
        self.on_tool_start(data)
        self._spans[tool_use_id].set_attributes(
            clean(meta(denied=True, denial=redact(reason, ERROR_CHARS)))
        )
        self._finish(tool_use_id, owner)

    @_never_raises
    def on_subagent_start(self, data: dict[str, Any]) -> None:
        agent_id, agent_type = data.get("agent_id"), data.get("agent_type")
        queue = self._unclaimed.get(str(agent_type), [])
        if not agent_id or not queue:
            return
        tool_use_id = queue.pop(0)
        self._agents[agent_id] = tool_use_id
        self._spans[tool_use_id].set_attributes(clean(meta(agent_id=agent_id)))

    @_never_raises
    def close(self) -> None:
        """Flush every pending turn, then mark whatever tool call is still open as ERROR.

        Safe to call twice.
        """
        for key in list(self._pending):
            self._flush(key)
        for tool_use_id in list(self._open):
            self._spans[tool_use_id].set_status(Status(StatusCode.ERROR, _UNFINISHED))
            self._finish(tool_use_id, None)

    def _start_llm(self, key: str | None, message: AssistantMessage, now: int) -> None:
        """Open a pending llm span from the first copy of a message, counting its usage."""
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
                timing="approximate",
            ),
        }
        start = self._last.get(key, now)
        llm = tracer().start_span(
            "llm", context=self._parent(key), attributes=clean(attrs), start_time=start
        )
        self._pending[key] = _PendingTurn(span=llm)

    def _flush(self, key: str | None) -> None:
        """End the key's pending llm span, if any, with everything its copies accumulated."""
        pending = self._pending.pop(key, None)
        if pending is None:
            return
        attrs: dict[str, AttributeValue] = meta(stop_reason=pending.stop_reason)
        if self._content:
            text = "\n".join(pending.texts)
            if text:
                attrs["output.value"] = text
        pending.span.set_attributes(clean(attrs))
        pending.span.end(end_time=pending.last)
        # A tool call the turn waited on may have ended after its last copy.
        self._last[key] = max(self._last.get(key, 0), pending.last)

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
