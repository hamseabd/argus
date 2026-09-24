"""Runs one query and turns the message stream into a result and metrics.

The runner is the only place that iterates SDK messages. It hands back the
structured output and a StageMetrics, and it converts every failure mode
into a typed error: a non-success result subtype, an API error hidden
under a success subtype, a success with no structured output, a stream
that ends without a result, and an exception raised by the SDK itself
(which is how a budget overrun surfaces).
"""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    RateLimitEvent,
    ResultMessage,
    query,
)

from argus.agent.hooks import HookState
from argus.agent.ledger import AgentLedger
from argus.domain.errors import AgentRunError, ReviewProtocolError
from argus.domain.models import StageMetrics
from argus.telemetry import bind_run, get_logger
from argus.tracing import redact

QueryFn = Callable[..., AsyncIterator[Any]]

_DETAIL_CHARS = 300


@dataclass(frozen=True)
class RunResult:
    structured_output: Any
    metrics: StageMetrics
    session_id: str


class Runner(Protocol):
    async def run(
        self, prompt: str, options: ClaudeAgentOptions, *, stage: str, state: HookState
    ) -> RunResult: ...


class SdkRunner:
    """Runs queries through claude_agent_sdk.query; the query function is injectable."""

    def __init__(self, query_fn: QueryFn = query) -> None:
        self._query = query_fn

    async def run(
        self, prompt: str, options: ClaudeAgentOptions, *, stage: str, state: HookState
    ) -> RunResult:
        log = get_logger()
        model = options.model or "default"
        log.info("stage_start", stage=stage, model=model)
        result: ResultMessage | None = None
        ledger = AgentLedger()
        try:
            # The result message is always last; do not break out of the loop early,
            # closing the generator before it finishes raises from its aclose().
            async for message in self._query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
                    bind_run(session_id=message.session_id)
                elif isinstance(message, AssistantMessage):
                    ledger.record(message)
                elif isinstance(message, RateLimitEvent):
                    info = message.rate_limit_info
                    log.warning(
                        "rate_limited",
                        stage=stage,
                        status=info.status,
                        rate_limit_type=info.rate_limit_type,
                        utilization=info.utilization,
                        resets_at=info.resets_at,
                    )
        except ClaudeSDKError as exc:
            raise AgentRunError(
                subtype=f"sdk_error:{type(exc).__name__}",
                cost_usd=result.total_cost_usd or 0.0 if result else 0.0,
                session_id=result.session_id if result else None,
                detail=_why(exc),
            ) from exc
        if result is None:
            raise ReviewProtocolError(f"{stage}: the query ended without a result message")
        cost = result.total_cost_usd or 0.0
        if result.subtype != "success" or result.is_error:
            subtype = result.subtype
            if subtype == "success":
                subtype = f"api_error:{result.api_error_status}"
            raise AgentRunError(subtype=subtype, cost_usd=cost, session_id=result.session_id)
        if result.structured_output is None:
            raise ReviewProtocolError(
                f"{stage}: the query succeeded but returned no structured output",
                cost_usd=cost,
                session_id=result.session_id,
            )
        usage = result.usage or {}
        metrics = StageMetrics(
            stage=stage,
            model=model,
            cost_usd=cost,
            input_tokens=usage.get("input_tokens") or 0,
            output_tokens=usage.get("output_tokens") or 0,
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens") or 0,
            cache_read_input_tokens=usage.get("cache_read_input_tokens") or 0,
            num_turns=result.num_turns,
            duration_ms=result.duration_ms,
            subagents_run=state.subagents_started,
            output_rejections=state.output_rejections,
            agents=ledger.metrics(state),
        )
        log.info("stage_end", **metrics.model_dump())
        return RunResult(
            structured_output=result.structured_output,
            metrics=metrics,
            session_id=result.session_id,
        )


def _why(exc: ClaudeSDKError) -> str | None:
    """What the SDK can tell us about a failure, in one line, minus anything token-shaped."""
    parts: list[str] = []
    if status := getattr(exc, "api_error_status", None):
        parts.append(f"api status {status}")
    if reason := getattr(exc, "terminal_reason", None):
        parts.append(str(reason))
    if stderr := (getattr(exc, "stderr", None) or "").strip():
        parts.append(stderr.splitlines()[-1])
    if not parts and (text := str(exc).strip()):
        parts.append(text)
    detail = redact("; ".join(parts), _DETAIL_CHARS)
    return detail or None
