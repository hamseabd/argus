import asyncio
import re

import pytest

from argus.agent.hooks import (
    ALLOWED_TOOLS,
    DENIED_TOOL_MATCHER,
    DENIED_TOOLS,
    LEAD_AGENT,
    READ_TOOL_MATCHER,
    READ_TOOLS,
    STRUCTURED_OUTPUT_TOOL,
    HookState,
    build_hooks,
    deny_mutating_tools,
    limit_lead_reading,
)


def hook_input(name: str, event: str = "PreToolUse", **extra) -> dict:
    return {
        "hook_event_name": event,
        "session_id": "s",
        "transcript_path": "/t",
        "cwd": "/r",
        "tool_name": name,
        "tool_input": {"command": "rm -rf /"} if name == "Bash" else {"file_path": "x"},
        "tool_use_id": "tu-1",
        **extra,
    }


@pytest.mark.parametrize("name", sorted(DENIED_TOOLS))
def test_every_mutating_tool_is_denied_with_a_reason(name: str) -> None:
    state = HookState()
    out = asyncio.run(deny_mutating_tools(state)(hook_input(name), None, {"signal": None}))

    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert name in specific["permissionDecisionReason"]
    assert "read-only" in specific["permissionDecisionReason"]


@pytest.mark.parametrize("name", sorted(ALLOWED_TOOLS))
def test_read_only_tools_pass_through(name: str) -> None:
    state = HookState()
    out = asyncio.run(deny_mutating_tools(state)(hook_input(name), None, {"signal": None}))

    assert out == {}


def test_matcher_regex_covers_exactly_the_denied_set() -> None:
    pattern = re.compile(f"^({DENIED_TOOL_MATCHER})$")

    for name in DENIED_TOOLS:
        assert pattern.match(name), name
    for name in ALLOWED_TOOLS:
        assert not pattern.match(name), name


def test_denials_are_counted_in_state() -> None:
    state = HookState()
    hook = deny_mutating_tools(state)
    asyncio.run(hook(hook_input("Bash"), None, {"signal": None}))
    asyncio.run(hook(hook_input("Write"), None, {"signal": None}))
    asyncio.run(hook(hook_input("Read"), None, {"signal": None}))

    assert state.denied == 2
    assert state.tool_calls == 0


def test_subagent_start_and_stop_are_counted() -> None:
    state = HookState()
    hooks = build_hooks(state)
    start = hooks["SubagentStart"][0].hooks[0]
    stop = hooks["SubagentStop"][0].hooks[0]
    base = {"session_id": "s", "transcript_path": "/t", "cwd": "/r"}

    for agent in ("correctness", "security", "quality"):
        asyncio.run(
            start(
                {
                    **base,
                    "hook_event_name": "SubagentStart",
                    "agent_id": agent,
                    "agent_type": agent,
                },
                None,
                {"signal": None},
            )
        )
    asyncio.run(
        stop(
            {
                **base,
                "hook_event_name": "SubagentStop",
                "agent_id": "security",
                "agent_type": "security",
                "stop_hook_active": False,
                "agent_transcript_path": "/x",
            },
            None,
            {"signal": None},
        )
    )

    assert state.subagents_started == 3
    assert state.subagents_stopped == 1


def test_post_tool_use_counts_calls_and_failures() -> None:
    state = HookState()
    hooks = build_hooks(state)
    post = hooks["PostToolUse"][0].hooks[0]
    failed = hooks["PostToolUseFailure"][0].hooks[0]

    asyncio.run(post(hook_input("Read", "PostToolUse", tool_response="ok"), None, {"signal": None}))
    asyncio.run(
        failed(hook_input("Grep", "PostToolUseFailure", error="boom"), None, {"signal": None})
    )

    assert state.tool_calls == 1
    assert state.tool_failures == 1
    assert state.output_rejections == 0


def test_rejected_structured_outputs_are_counted_separately() -> None:
    state = HookState()
    failed = build_hooks(state)["PostToolUseFailure"][0].hooks[0]
    rejected = hook_input(
        "StructuredOutput", "PostToolUseFailure", error="Output does not match required schema"
    )

    asyncio.run(failed(rejected, None, {"signal": None}))
    asyncio.run(failed(rejected, None, {"signal": None}))

    assert state.output_rejections == 2
    assert state.tool_failures == 2


def test_tool_calls_are_attributed_to_the_subagent_that_made_them() -> None:
    state = HookState()
    hooks = build_hooks(state)
    post = hooks["PostToolUse"][0].hooks[0]
    failed = hooks["PostToolUseFailure"][0].hooks[0]
    inside = {"agent_id": "a-sec", "agent_type": "security"}

    asyncio.run(post(hook_input("Read", "PostToolUse", **inside), None, {"signal": None}))
    asyncio.run(post(hook_input("Grep", "PostToolUse", **inside), None, {"signal": None}))
    asyncio.run(failed(hook_input("Read", "PostToolUseFailure", **inside), None, {"signal": None}))
    asyncio.run(post(hook_input("Glob", "PostToolUse"), None, {"signal": None}))  # main thread

    assert state.agents["a-sec"].tool_calls == 2
    assert state.agents["a-sec"].tool_failures == 1
    assert state.agents[LEAD_AGENT].tool_calls == 1
    assert state.agent_types == {"a-sec": "security"}
    assert state.tool_calls == 3  # the totals still count everyone


def test_subagent_duration_is_measured_from_start_to_stop() -> None:
    ticks = iter([10.0, 12.5])
    state = HookState(clock=lambda: next(ticks))
    hooks = build_hooks(state)
    start = hooks["SubagentStart"][0].hooks[0]
    stop = hooks["SubagentStop"][0].hooks[0]
    agent = {"agent_id": "a-q", "agent_type": "quality"}

    asyncio.run(start({"hook_event_name": "SubagentStart", **agent}, None, {"signal": None}))
    asyncio.run(stop({"hook_event_name": "SubagentStop", **agent}, None, {"signal": None}))

    assert state.agents["a-q"].duration_ms == 2500
    assert state.agent_types["a-q"] == "quality"


def test_the_lead_may_read_up_to_its_budget() -> None:
    state = HookState(read_budget=2)
    hook = limit_lead_reading(state)

    assert asyncio.run(hook(hook_input("Read"), None, {"signal": None})) == {}
    assert asyncio.run(hook(hook_input("Grep"), None, {"signal": None})) == {}
    assert state.lead_reads == 2
    assert state.reads_denied == 0


def test_a_read_past_the_budget_is_denied_and_says_what_to_do_instead() -> None:
    state = HookState(read_budget=1)
    hook = limit_lead_reading(state)
    asyncio.run(hook(hook_input("Read"), None, {"signal": None}))

    out = asyncio.run(hook(hook_input("Read"), None, {"signal": None}))

    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert "specialists" in specific["permissionDecisionReason"]
    assert state.reads_denied == 1
    assert state.lead_reads == 1  # a refused read is not spent
    assert state.denied == 0  # that counter is for mutating tools


def test_the_budget_never_blocks_delegation_or_the_final_answer() -> None:
    state = HookState(read_budget=0)
    hook = limit_lead_reading(state)

    assert asyncio.run(hook(hook_input("Agent"), None, {"signal": None})) == {}
    assert asyncio.run(hook(hook_input(STRUCTURED_OUTPUT_TOOL), None, {"signal": None})) == {}
    assert state.reads_denied == 0


def test_a_specialist_reads_on_its_own_budget_not_the_lead_s() -> None:
    state = HookState(read_budget=1)
    hook = limit_lead_reading(state)
    inside = {"agent_id": "a-sec", "agent_type": "security"}

    for _ in range(3):
        assert asyncio.run(hook(hook_input("Read", **inside), None, {"signal": None})) == {}

    assert state.lead_reads == 0
    assert state.reads_denied == 0


def test_without_a_budget_the_main_thread_reads_freely() -> None:
    state = HookState()  # the verifier: reading the code is its whole job
    hook = limit_lead_reading(state)

    for _ in range(20):
        assert asyncio.run(hook(hook_input("Read"), None, {"signal": None})) == {}

    assert state.reads_denied == 0


def test_the_read_matcher_covers_every_read_tool_and_nothing_else() -> None:
    pattern = re.compile(f"^({READ_TOOL_MATCHER})$")

    for name in READ_TOOLS:
        assert pattern.match(name), name
    for name in ("Agent", STRUCTURED_OUTPUT_TOOL, *DENIED_TOOLS):
        assert not pattern.match(name), name


def test_build_hooks_registers_the_expected_events_and_matchers() -> None:
    hooks = build_hooks(HookState())

    assert set(hooks) == {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SubagentStart",
        "SubagentStop",
    }
    assert [m.matcher for m in hooks["PreToolUse"]] == [DENIED_TOOL_MATCHER, READ_TOOL_MATCHER]
    assert hooks["PostToolUse"][0].matcher is None
    for matchers in hooks.values():
        for matcher in matchers:
            assert matcher.hooks


def test_hook_events_are_logged(capsys: pytest.CaptureFixture[str]) -> None:
    import io
    import json

    from argus import telemetry

    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    state = HookState()
    asyncio.run(deny_mutating_tools(state)(hook_input("Bash"), None, {"signal": None}))

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert events[-1]["event"] == "tool_denied"
    assert events[-1]["tool"] == "Bash"
    assert "rm -rf" in events[-1]["input"]
