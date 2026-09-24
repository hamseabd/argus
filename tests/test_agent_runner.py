import io
import json
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    ResultError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from argus import telemetry
from argus.agent import trace as trace_module
from argus.agent.hooks import LEAD_AGENT, AgentCounters, HookState, build_hooks
from argus.agent.runner import RunResult, SdkRunner
from argus.domain.errors import AgentRunError, ReviewProtocolError
from argus.domain.models import AgentMetrics
from argus.tracing import span


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


def test_a_failed_run_says_why_not_only_that_it_failed() -> None:
    """The reason lives on the exception; without it CI shows a bare error name."""
    failure = ResultError(
        "run failed",
        data={
            "subtype": "error_during_execution",
            "api_error_status": 401,
            "terminal_reason": "api_error",
        },
        exit_code=1,
    )

    with pytest.raises(AgentRunError) as info:
        asyncio_run(run(runner_for(failure)))

    message = str(info.value)
    assert "401" in message
    assert "api_error" in message
    assert info.value.detail


def test_a_credential_in_the_failure_is_redacted() -> None:
    """Review logs are public on a public repository, so nothing token-shaped goes in one."""
    leaked = "sk-ant-oat01-" + "A1b2C3d4" * 11
    failure = ProcessError("cli died", exit_code=1, stderr=f"invalid token {leaked}")

    with pytest.raises(AgentRunError) as info:
        asyncio_run(run(runner_for(failure)))

    assert leaked not in str(info.value)
    assert "[redacted]" in str(info.value)


@pytest.mark.parametrize(
    "credential",
    [
        "sk-ant-oat01-" + "A1b2C3d4" * 11,
        "ghs_" + "A1b2C3d4" * 5,  # the app installation token the review step is given
        "ghp_" + "A1b2C3d4" * 5,
        "github_pat_" + "A1b2C3d4" * 8,
    ],
)
def test_every_credential_shape_in_reach_is_redacted(credential: str) -> None:
    failure = ProcessError("cli died", exit_code=1, stderr=f"rejected: {credential}")

    with pytest.raises(AgentRunError) as info:
        asyncio_run(run(runner_for(failure)))

    assert credential not in str(info.value)
    assert "[redacted]" in str(info.value)


def test_a_wall_of_cli_output_cannot_become_the_error_message() -> None:
    failure = ProcessError("cli died", exit_code=1, stderr="x" * 5000)

    with pytest.raises(AgentRunError) as info:
        asyncio_run(run(runner_for(failure)))

    assert len(info.value.detail or "") <= 300


def test_a_failure_with_nothing_structured_still_reports_its_message() -> None:
    with pytest.raises(AgentRunError) as info:
        asyncio_run(run(runner_for(CLIConnectionError("cannot reach the CLI"))))

    assert "cannot reach the CLI" in str(info.value)


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


def traced_runner(script, trace_content: bool = False) -> SdkRunner:
    """A fake query that interleaves stream messages with hook calls, as the CLI does."""

    async def fake_query(*, prompt: str, options: ClaudeAgentOptions):
        for step in script:
            if isinstance(step, tuple):
                event, index, data = step
                await options.hooks[event][index].hooks[0](data, None, {"signal": None})
            elif isinstance(step, Exception):
                raise step
            else:
                yield step

    return SdkRunner(query_fn=fake_query, trace_content=trace_content)


def hooked_options(state: HookState) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(model="claude-opus-5", hooks=build_hooks(state))


def test_a_query_becomes_a_span_tree_under_the_current_stage(spans) -> None:
    state = HookState()

    def lead() -> dict:
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_use_id": "tu-a",
            "tool_input": {"subagent_type": "security"},
        }

    sub = {"agent_id": "ag-1", "agent_type": "security"}
    read = {"tool_name": "Read", "tool_use_id": "tu-r", "tool_input": {"file_path": "a.py"}, **sub}
    script = [
        AssistantMessage(
            content=[ToolUseBlock(id="tu-a", name="Agent", input={"subagent_type": "security"})],
            model="claude-opus-5",
            message_id="m1",
            usage={"output_tokens": 5},
        ),
        ("PreToolUse", -1, lead()),
        ("SubagentStart", 0, {"hook_event_name": "SubagentStart", **sub}),
        AssistantMessage(
            content=[TextBlock("reading")],
            model="claude-sonnet-5",
            parent_tool_use_id="tu-a",
            message_id="m2",
            usage={"output_tokens": 7},
        ),
        ("PreToolUse", -1, {"hook_event_name": "PreToolUse", **read}),
        ("PostToolUse", 0, {"hook_event_name": "PostToolUse", **read}),
        ("PostToolUse", 0, {**lead(), "hook_event_name": "PostToolUse"}),
        result(),
    ]

    with span("argus.review", "chain") as stage:
        asyncio_run(
            traced_runner(script).run("p", hooked_options(state), stage="review", state=state)
        )

    finished = spans.get_finished_spans()
    names = {s.name: s for s in finished}
    assert names["security"].parent.span_id == stage.get_span_context().span_id
    assert names["Read"].parent.span_id == names["security"].context.span_id
    assert sum(s.name == "llm" for s in finished) == 2
    assert state.recorder is None


def test_a_failed_query_closes_its_spans_and_detaches_the_recorder(spans) -> None:
    state = HookState()
    read = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_use_id": "tu-r",
        "tool_input": {},
    }
    script = [("PreToolUse", -1, read), CLIConnectionError("gone")]

    with span("argus.review", "chain"), pytest.raises(AgentRunError):
        asyncio_run(
            traced_runner(script).run("p", hooked_options(state), stage="review", state=state)
        )

    (read_span,) = [s for s in spans.get_finished_spans() if s.name == "Read"]
    assert read_span.status.status_code.name == "ERROR"
    assert state.recorder is None


def test_content_mode_puts_the_prompt_and_output_on_the_stage(spans) -> None:
    state = HookState()
    with span("argus.review", "chain"):
        asyncio_run(
            traced_runner([result()], trace_content=True).run(
                "review ghp_abcdefghijkl1234", hooked_options(state), stage="review", state=state
            )
        )

    (stage,) = [s for s in spans.get_finished_spans() if s.name == "argus.review"]
    assert "[redacted]" in stage.attributes["input.value"]
    assert json.loads(stage.attributes["output.value"])["verdict"] == "confirmed"


def test_no_content_on_the_stage_by_default(spans) -> None:
    state = HookState()
    with span("argus.review", "chain"):
        asyncio_run(
            traced_runner([result()]).run("p", hooked_options(state), stage="review", state=state)
        )

    (stage,) = [s for s in spans.get_finished_spans() if s.name == "argus.review"]
    assert "input.value" not in stage.attributes


def test_a_run_survives_a_recorder_whose_internals_are_broken(monkeypatch, spans) -> None:
    """A tracing bug must never fail the review: the run still returns its RunResult."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(trace_module, "tracer", boom)
    state = HookState()
    read = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_use_id": "tu-r",
        "tool_input": {},
    }
    script = [
        AssistantMessage(content=[TextBlock("hi")], model="claude-opus-5", message_id="m1"),
        ("PreToolUse", -1, read),
        ("PostToolUse", 0, {**read, "hook_event_name": "PostToolUse"}),
        result(),
    ]

    with span("argus.review", "chain"):
        out = asyncio_run(
            traced_runner(script).run("p", hooked_options(state), stage="review", state=state)
        )

    assert out.structured_output == {"verdict": "confirmed", "reasoning": "r", "confidence": 0.9}
    assert state.recorder is None
