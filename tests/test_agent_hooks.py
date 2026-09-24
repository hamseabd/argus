import asyncio
import re

import pytest

from argus.agent import trace as trace_module
from argus.agent.hooks import (
    ALLOWED_TOOLS,
    DENIED_TOOL_MATCHER,
    DENIED_TOOLS,
    LEAD_AGENT,
    READ_TOOL_MATCHER,
    READ_TOOLS,
    STRUCTURED_OUTPUT_TOOL,
    HookState,
    audit_tool_call,
    build_hooks,
    deny_mutating_tools,
    limit_lead_reading,
)
from argus.agent.trace import TraceRecorder


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
    assert [m.matcher for m in hooks["PreToolUse"]] == [
        DENIED_TOOL_MATCHER,
        READ_TOOL_MATCHER,
        STRUCTURED_OUTPUT_TOOL,
        None,
    ]
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


def test_a_credential_shaped_tool_call_input_is_redacted_in_the_log() -> None:
    import io
    import json

    from argus import telemetry

    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    state = HookState()
    data = hook_input("Grep", event="PostToolUse", tool_input={"pattern": "ghp_abcdefghijkl1234"})
    asyncio.run(audit_tool_call(state)(data, None, {"signal": None}))

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert events[-1]["event"] == "tool_call"
    assert "ghp_abcdefghijkl1234" not in events[-1]["input"]
    assert "[redacted]" in events[-1]["input"]


def test_a_credential_shaped_denied_tool_input_is_redacted_in_the_log() -> None:
    import io
    import json

    from argus import telemetry

    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    state = HookState()
    data = hook_input(
        "Bash", tool_input={"command": "curl -H 'Authorization: ghp_abcdefghijkl1234'"}
    )
    asyncio.run(deny_mutating_tools(state)(data, None, {"signal": None}))

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert events[-1]["event"] == "tool_denied"
    assert "ghp_abcdefghijkl1234" not in events[-1]["input"]
    assert "[redacted]" in events[-1]["input"]


class RecorderSpy:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def on_tool_start(self, data):
        self.calls.append(("start", data["tool_use_id"]))

    def on_tool_end(self, data, error=None):
        self.calls.append(("end", data["tool_use_id"], error))

    def on_tool_denied(self, data, reason):
        self.calls.append(("denied", data["tool_use_id"]))

    def on_subagent_start(self, data):
        self.calls.append(("subagent", data["agent_id"]))


def test_hooks_feed_the_recorder_when_one_is_attached() -> None:
    state = HookState(read_budget=0)
    state.recorder = RecorderSpy()
    hooks = build_hooks(state)
    ctx = {"signal": None}
    observe = hooks["PreToolUse"][-1].hooks[0]

    asyncio.run(observe(hook_input("Grep", tool_use_id="tu-1"), None, ctx))
    asyncio.run(
        hooks["PostToolUse"][0].hooks[0](
            hook_input("Grep", "PostToolUse", tool_use_id="tu-1"), None, ctx
        )
    )
    asyncio.run(
        hooks["PostToolUseFailure"][0].hooks[0](
            hook_input("Read", "PostToolUseFailure", tool_use_id="tu-2", error="boom"), None, ctx
        )
    )
    asyncio.run(deny_mutating_tools(state)(hook_input("Bash", tool_use_id="tu-3"), None, ctx))
    asyncio.run(limit_lead_reading(state)(hook_input("Read", tool_use_id="tu-4"), None, ctx))
    asyncio.run(
        hooks["SubagentStart"][0].hooks[0](
            {"hook_event_name": "SubagentStart", "agent_id": "ag-1", "agent_type": "security"},
            None,
            ctx,
        )
    )

    assert state.recorder.calls == [
        ("start", "tu-1"),
        ("end", "tu-1", None),
        ("end", "tu-2", "boom"),
        ("denied", "tu-3"),
        ("denied", "tu-4"),
        ("subagent", "ag-1"),
    ]


def test_hooks_run_without_a_recorder() -> None:
    state = HookState()
    observe = build_hooks(state)["PreToolUse"][-1].hooks[0]

    assert asyncio.run(observe(hook_input("Grep"), None, {"signal": None})) == {}


def test_a_denial_still_comes_back_even_if_the_recorder_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracing is best-effort; a broken recorder must never swallow a real deny."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(trace_module, "tracer", boom)
    state = HookState(read_budget=1)
    state.recorder = TraceRecorder()  # a real recorder, its internals now broken

    out = asyncio.run(deny_mutating_tools(state)(hook_input("Bash"), None, {"signal": None}))
    specific = out["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "read-only" in specific["permissionDecisionReason"]

    hook = limit_lead_reading(state)
    asyncio.run(hook(hook_input("Read"), None, {"signal": None}))  # spends the one read
    past_budget = asyncio.run(hook(hook_input("Read"), None, {"signal": None}))
    specific2 = past_budget["hookSpecificOutput"]
    assert specific2["permissionDecision"] == "deny"
    assert "specialists" in specific2["permissionDecisionReason"]


def test_the_lead_cannot_answer_while_a_specialist_is_still_running() -> None:
    state = HookState()
    hooks = build_hooks(state)
    ctx = {"signal": None}
    start = hooks["SubagentStart"][0].hooks[0]
    stop = hooks["SubagentStop"][0].hooks[0]
    answer = {
        "hook_event_name": "PreToolUse",
        "tool_name": STRUCTURED_OUTPUT_TOOL,
        "tool_use_id": "so-1",
        "tool_input": {},
    }

    def pre_tool(data: dict) -> list[dict]:
        return [
            asyncio.run(m.hooks[0](data, None, ctx))
            for m in hooks["PreToolUse"]
            if m.matcher is None or re.fullmatch(m.matcher, data["tool_name"])
        ]

    for agent_id, agent_type in (("a-1", "correctness"), ("a-2", "security")):
        asyncio.run(
            start(
                {
                    "hook_event_name": "SubagentStart",
                    "agent_id": agent_id,
                    "agent_type": agent_type,
                },
                None,
                ctx,
            )
        )
    asyncio.run(
        stop(
            {"hook_event_name": "SubagentStop", "agent_id": "a-1", "agent_type": "correctness"},
            None,
            ctx,
        )
    )

    denied = [o for o in pre_tool(answer) if o]
    assert len(denied) == 1
    specific = denied[0]["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "security" in specific["permissionDecisionReason"]
    assert "correctness" not in specific["permissionDecisionReason"]

    asyncio.run(
        stop(
            {"hook_event_name": "SubagentStop", "agent_id": "a-2", "agent_type": "security"},
            None,
            ctx,
        )
    )
    assert [o for o in pre_tool(answer) if o] == []


def test_the_answer_is_never_held_when_no_specialist_ran() -> None:
    state = HookState()
    hooks = build_hooks(state)
    answer = {
        "hook_event_name": "PreToolUse",
        "tool_name": STRUCTURED_OUTPUT_TOOL,
        "tool_use_id": "so-1",
        "tool_input": {},
    }

    outs = [
        asyncio.run(m.hooks[0](answer, None, {"signal": None}))
        for m in hooks["PreToolUse"]
        if m.matcher is None or re.fullmatch(m.matcher, STRUCTURED_OUTPUT_TOOL)
    ]

    assert all(o == {} for o in outs)
