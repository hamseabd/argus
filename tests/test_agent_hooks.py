import asyncio
import re

import pytest

from argus.agent.hooks import (
    ALLOWED_TOOLS,
    DENIED_TOOL_MATCHER,
    DENIED_TOOLS,
    HookState,
    build_hooks,
    deny_mutating_tools,
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


def test_build_hooks_registers_the_expected_events_and_matchers() -> None:
    hooks = build_hooks(HookState())

    assert set(hooks) == {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SubagentStart",
        "SubagentStop",
    }
    assert hooks["PreToolUse"][0].matcher == DENIED_TOOL_MATCHER
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
