import io
import json
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from argus import telemetry
from argus.agent.hooks import LEAD_AGENT, AgentCounters, HookState
from argus.agent.runner import RunResult, SdkRunner
from argus.domain.errors import AgentRunError, ReviewProtocolError
from argus.domain.models import AgentMetrics


def result(**overrides: Any) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=1200,
        duration_api_ms=1000,
        is_error=False,
        num_turns=3,
        session_id="sess-1",
        total_cost_usd=0.42,
        usage={
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation_input_tokens": 300,
            "cache_read_input_tokens": 4000,
        },
        result="done",
        structured_output={"verdict": "confirmed", "reasoning": "r", "confidence": 0.9},
    )
    return ResultMessage(**{**base, **overrides})


def runner_for(messages: list[Any] | Exception) -> SdkRunner:
    async def fake_query(*, prompt: str, options: ClaudeAgentOptions):
        if isinstance(messages, Exception):
            raise messages
        for message in messages:
            yield message

    return SdkRunner(query_fn=fake_query)


def options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(model="claude-sonnet-5")


async def run(runner: SdkRunner, state: HookState | None = None) -> RunResult:
    return await runner.run("prompt", options(), stage="verify:x", state=state or HookState())


def test_success_returns_structured_output_and_metrics() -> None:
    state = HookState(subagents_started=3, output_rejections=2)
    out = asyncio_run(
        run(runner_for([AssistantMessage(content=[TextBlock("hi")], model="m"), result()]), state)
    )

    assert out.structured_output == {"verdict": "confirmed", "reasoning": "r", "confidence": 0.9}
    assert out.session_id == "sess-1"
    m = out.metrics
    assert m.stage == "verify:x"
    assert m.model == "claude-sonnet-5"
    assert m.cost_usd == 0.42
    assert m.input_tokens == 10
    assert m.output_tokens == 20
    assert m.cache_creation_input_tokens == 300
    assert m.cache_read_input_tokens == 4000
    assert m.num_turns == 3
    assert m.duration_ms == 1200
    assert m.subagents_run == 3
    assert m.output_rejections == 2


def test_metrics_attribute_turns_and_tokens_to_the_lead_and_each_specialist() -> None:
    delegate = AssistantMessage(
        content=[
            ToolUseBlock(id="tu-sec", name="Agent", input={"subagent_type": "security"}),
            ToolUseBlock(id="tu-cor", name="Agent", input={"subagent_type": "correctness"}),
        ],
        model="claude-opus-5",
        usage={"output_tokens": 50, "cache_read_input_tokens": 1000},
    )
    security_turn = AssistantMessage(
        content=[TextBlock("reading")],
        model="claude-sonnet-5",
        parent_tool_use_id="tu-sec",
        usage={
            "output_tokens": 20,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 5,
        },
    )
    final = AssistantMessage(
        content=[TextBlock("done")], model="claude-opus-5", usage={"output_tokens": 30}
    )
    state = HookState(
        agents={
            LEAD_AGENT: AgentCounters(tool_calls=1),
            "a-sec": AgentCounters(tool_calls=7, tool_failures=1, duration_ms=4200),
        },
        agent_types={"a-sec": "security"},
    )

    out = asyncio_run(
        run(runner_for([delegate, security_turn, security_turn, final, result()]), state)
    )

    assert out.metrics.agents == [
        AgentMetrics(
            agent="lead", turns=2, tool_calls=1, output_tokens=80, cache_read_input_tokens=1000
        ),
        AgentMetrics(
            agent="security",
            turns=2,
            tool_calls=7,
            tool_failures=1,
            output_tokens=40,
            cache_read_input_tokens=600,
            cache_creation_input_tokens=10,
            duration_ms=4200,
        ),
        AgentMetrics(agent="correctness"),  # delegated to, never spoke: still listed
    ]


def test_error_subtype_raises_agent_run_error_with_cost_and_session() -> None:
    with pytest.raises(AgentRunError) as info:
        asyncio_run(run(runner_for([result(subtype="error_max_turns", is_error=True)])))

    assert info.value.subtype == "error_max_turns"
    assert info.value.cost_usd == 0.42
    assert info.value.session_id == "sess-1"


def test_structured_output_retry_exhaustion_is_an_agent_run_error() -> None:
    with pytest.raises(AgentRunError, match="error_max_structured_output_retries"):
        asyncio_run(
            run(runner_for([result(subtype="error_max_structured_output_retries", is_error=True)]))
        )


def test_api_error_under_a_success_subtype_is_still_an_error() -> None:
    with pytest.raises(AgentRunError, match="api_error:529"):
        asyncio_run(run(runner_for([result(is_error=True, api_error_status=529)])))


def test_success_without_structured_output_is_a_protocol_error_that_carries_cost() -> None:
    with pytest.raises(ReviewProtocolError) as info:
        asyncio_run(run(runner_for([result(structured_output=None)])))

    assert info.value.cost_usd == 0.42
    assert info.value.session_id == "sess-1"


def test_no_result_message_is_a_protocol_error() -> None:
    with pytest.raises(ReviewProtocolError):
        asyncio_run(run(runner_for([AssistantMessage(content=[], model="m")])))


def test_sdk_exceptions_become_agent_run_errors() -> None:
    with pytest.raises(AgentRunError) as info:
        asyncio_run(run(runner_for(ProcessError("cli died", exit_code=1, stderr="boom"))))

    assert info.value.subtype == "sdk_error:ProcessError"
    assert info.value.cost_usd == 0.0
    assert "boom" in str(info.value.__cause__)


def test_missing_usage_and_cost_default_to_zero() -> None:
    out = asyncio_run(run(runner_for([result(usage=None, total_cost_usd=None)])))

    assert out.metrics.cost_usd == 0.0
    assert out.metrics.input_tokens == 0


def test_rate_limits_and_stages_are_logged() -> None:
    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    event = RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="allowed_warning", rate_limit_type="five_hour", utilization=0.9
        ),
        uuid="u",
        session_id="sess-1",
    )

    asyncio_run(run(runner_for([event, result()])))

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [r["event"] for r in records] == ["stage_start", "rate_limited", "stage_end"]
    assert records[1]["status"] == "allowed_warning"
    assert records[1]["utilization"] == 0.9
    last = json.loads(stream.getvalue().splitlines()[-1])
    assert last["cost_usd"] == 0.42
    assert last["stage"] == "verify:x"
    assert last["session_id"] == "sess-1"


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
