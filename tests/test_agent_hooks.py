import asyncio
import io
import json
import re
from pathlib import Path

import pytest

from argus import telemetry
from argus.agent import trace as trace_module
from argus.agent.findings import FindingCollector
from argus.agent.hooks import (
    ALLOWED_TOOLS,
    DENIED_TOOL_MATCHER,
    DENIED_TOOLS,
    LEAD_AGENT,
    PATH_TOOL_MATCHER,
    READ_TOOL_MATCHER,
    READ_TOOLS,
    REPORT_REFUSAL_LIMIT,
    STRUCTURED_OUTPUT_TOOL,
    HookState,
    audit_tool_call,
    await_specialists,
    build_hooks,
    check_reports,
    deny_mutating_tools,
    limit_lead_reading,
)
from argus.agent.tools import REPORT_FINDING_TOOL_NAME
from argus.agent.trace import TraceRecorder

REPO = Path("/repo")


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
    hooks = build_hooks(state, REPO)
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
    hooks = build_hooks(state, REPO)
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
    failed = build_hooks(state, REPO)["PostToolUseFailure"][0].hooks[0]
    rejected = hook_input(
        "StructuredOutput", "PostToolUseFailure", error="Output does not match required schema"
    )

    asyncio.run(failed(rejected, None, {"signal": None}))
    asyncio.run(failed(rejected, None, {"signal": None}))

    assert state.output_rejections == 2
    assert state.tool_failures == 2


def test_tool_calls_are_attributed_to_the_subagent_that_made_them() -> None:
    state = HookState()
    hooks = build_hooks(state, REPO)
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
    hooks = build_hooks(state, REPO)
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
    hooks = build_hooks(HookState(), REPO)

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
        PATH_TOOL_MATCHER,
        REPORT_FINDING_TOOL_NAME,
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
    hooks = build_hooks(state, REPO)
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
    observe = build_hooks(state, REPO)["PreToolUse"][-1].hooks[0]

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
    hooks = build_hooks(state, REPO)
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
    # SubagentStop fires before the report reaches the lead: one more round for it.
    (last_round,) = [o for o in pre_tool(answer) if o]
    assert "security" in last_round["hookSpecificOutput"]["permissionDecisionReason"]
    assert state.answers_held == 2

    assert [o for o in pre_tool(answer) if o] == []


def test_the_answer_is_never_held_when_no_specialist_ran() -> None:
    state = HookState()
    hooks = build_hooks(state, REPO)
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


def test_the_lead_s_accepted_answer_is_kept() -> None:
    """A later turn can end in plain text; the accepted answer is what the runner recovers."""
    state = HookState()
    hook = audit_tool_call(state)
    review = {"summary": "s", "findings": [], "files_reviewed": []}

    asyncio.run(
        hook(
            hook_input(STRUCTURED_OUTPUT_TOOL, "PostToolUse", tool_input=review),
            None,
            {"signal": None},
        )
    )

    assert state.accepted_answer == review


def test_a_subagent_s_structured_output_is_not_the_lead_s_answer() -> None:
    state = HookState()
    hook = audit_tool_call(state)

    asyncio.run(
        hook(
            hook_input(
                STRUCTURED_OUTPUT_TOOL,
                "PostToolUse",
                tool_input={"x": 1},
                agent_id="a-1",
                agent_type="security",
            ),
            None,
            {"signal": None},
        )
    )

    assert state.accepted_answer is None


def test_a_rejected_answer_is_never_kept() -> None:
    state = HookState()
    hooks = build_hooks(state, REPO)
    failure = hooks["PostToolUseFailure"][0].hooks[0]

    asyncio.run(
        failure(
            hook_input(STRUCTURED_OUTPUT_TOOL, "PostToolUseFailure", tool_input={"summary": "x"}),
            None,
            {"signal": None},
        )
    )

    assert state.accepted_answer is None


def _answer() -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": STRUCTURED_OUTPUT_TOOL,
        "tool_use_id": "so-1",
        "tool_input": {},
    }


def _subagent(hooks, event: str, agent_id: str, agent_type: str) -> None:
    data = {"hook_event_name": event, "agent_id": agent_id, "agent_type": agent_type}
    asyncio.run(hooks[event][0].hooks[0](data, None, {"signal": None}))


def _held(state: HookState) -> bool:
    return bool(asyncio.run(await_specialists(state)(_answer(), None, {"signal": None})))


def test_the_answer_is_held_one_round_after_the_last_specialist_stops() -> None:
    state = HookState()
    hooks = build_hooks(state, REPO)
    _subagent(hooks, "SubagentStart", "a-1", "correctness")
    _subagent(hooks, "SubagentStop", "a-1", "correctness")

    assert _held(state) is True
    assert _held(state) is False
    assert state.answers_held == 1


def test_holds_are_bounded_so_a_stuck_specialist_cannot_hold_the_answer_forever() -> None:
    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    state = HookState(answer_hold_limit=2)
    hooks = build_hooks(state, REPO)
    _subagent(hooks, "SubagentStart", "a-1", "quality")

    assert [_held(state) for _ in range(3)] == [True, True, False]
    assert state.answers_held == 2
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    (released,) = [r for r in records if r["event"] == "answer_hold_released"]
    assert released["held"] == 2
    assert released["reason"] == "hold_limit"


def test_a_release_after_the_specialists_report_is_logged_with_the_count() -> None:
    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    state = HookState()
    hooks = build_hooks(state, REPO)
    _subagent(hooks, "SubagentStart", "a-1", "security")
    assert _held(state) is True
    _subagent(hooks, "SubagentStop", "a-1", "security")
    assert _held(state) is True
    assert _held(state) is False

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    (released,) = [r for r in records if r["event"] == "answer_hold_released"]
    assert released["held"] == 2
    assert released["reason"] == "reported"


def test_every_deny_path_records_the_refusal_on_the_trace() -> None:
    """The three refusals share _deny, so each path pins both of its effects: payload and span."""
    state = HookState(read_budget=0)
    state.recorder = RecorderSpy()
    hooks = build_hooks(state, REPO)
    _subagent(hooks, "SubagentStart", "a-1", "security")

    assert _held(state) is True
    read = asyncio.run(limit_lead_reading(state)(hook_input("Read"), None, {"signal": None}))
    write = asyncio.run(deny_mutating_tools(state)(hook_input("Write"), None, {"signal": None}))

    assert read["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert write["hookSpecificOutput"]["permissionDecision"] == "deny"
    denied = [call for call in state.recorder.calls if call[0] == "denied"]
    assert denied == [("denied", _answer()["tool_use_id"])] + [
        ("denied", hook_input(name)["tool_use_id"]) for name in ("Read", "Write")
    ]


def test_a_hold_limit_of_zero_never_holds() -> None:
    state = HookState(answer_hold_limit=0)
    hooks = build_hooks(state, REPO)
    _subagent(hooks, "SubagentStart", "a-1", "security")

    assert _held(state) is False


def test_a_recorded_finding_is_attributed_to_the_agent_that_reported_it(tmp_path) -> None:
    (tmp_path / "a.py").write_text("x\n")
    collector = FindingCollector(tmp_path)
    collector.add(
        {
            "file": "a.py",
            "line": 1,
            "end_line": None,
            "severity": "low",
            "category": "maintainability",
            "title": "t",
            "description": "twenty characters at least",
            "evidence": "twenty characters at least",
            "suggested_fix": None,
            "confidence": 0.5,
        }
    )
    state = HookState(collector=collector)
    post = audit_tool_call(state)

    asyncio.run(
        post(
            hook_input(
                REPORT_FINDING_TOOL_NAME,
                "PostToolUse",
                agent_id="a-q",
                agent_type="quality",
                tool_response=[{"type": "text", "text": "Recorded F1."}],
            ),
            None,
            {"signal": None},
        )
    )

    assert [f.reported_by for f in collector.findings()] == ["quality"]


def test_a_finding_the_lead_records_itself_is_attributed_to_the_lead(tmp_path) -> None:
    (tmp_path / "a.py").write_text("x\n")
    collector = FindingCollector(tmp_path)
    collector.add(
        {
            "file": "a.py",
            "line": 1,
            "end_line": None,
            "severity": "low",
            "category": "maintainability",
            "title": "t",
            "description": "twenty characters at least",
            "evidence": "twenty characters at least",
            "suggested_fix": None,
            "confidence": 0.5,
        }
    )
    state = HookState(collector=collector)

    asyncio.run(
        audit_tool_call(state)(
            hook_input(REPORT_FINDING_TOOL_NAME, "PostToolUse", tool_response="Recorded F1."),
            None,
            {"signal": None},
        )
    )

    assert collector.findings()[0].reported_by == LEAD_AGENT


def test_a_refused_report_attributes_nothing(tmp_path) -> None:
    collector = FindingCollector(tmp_path)
    state = HookState(collector=collector)
    refused = {
        "content": [{"type": "text", "text": "Not recorded: line: must be at least 1"}],
        "is_error": True,
    }

    asyncio.run(
        audit_tool_call(state)(
            hook_input(
                REPORT_FINDING_TOOL_NAME, "PostToolUse", agent_id="a-q", tool_response=refused
            ),
            None,
            {"signal": None},
        )
    )

    assert collector.findings() == []


def test_the_leads_refusals_point_it_at_the_triage() -> None:
    state = HookState(read_budget=0)
    out = asyncio.run(limit_lead_reading(state)(hook_input("Read"), None, {"signal": None}))

    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Triage" in reason and "Review" not in reason


REPORT = {
    "file": "a.py",
    "line": 1,
    "end_line": None,
    "severity": "low",
    "category": "maintainability",
    "title": "t",
    "description": "twenty characters at least",
    "evidence": "a.py:1: x, and the reasoning it supports",
    "suggested_fix": None,
    "replacement": None,
    "confidence": 0.5,
}
NO_EVIDENCE = {k: v for k, v in REPORT.items() if k != "evidence"}


def _report(state: HookState, tool_input: dict, agent_id: str = "a-sec") -> str | None:
    """The reason check_reports denies this report_finding call with, or None if it passes."""
    data = hook_input(
        REPORT_FINDING_TOOL_NAME, agent_id=agent_id, agent_type="security", tool_input=tool_input
    )
    out = asyncio.run(check_reports(state)(data, None, {"signal": None}))
    if not out:
        return None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    return str(out["hookSpecificOutput"]["permissionDecisionReason"])


def _reporting_state(tmp_path: Path) -> HookState:
    (tmp_path / "a.py").write_text("x\n")
    return HookState(collector=FindingCollector(tmp_path))


def test_a_report_without_evidence_is_denied_saying_what_evidence_must_hold(tmp_path) -> None:
    reason = _report(_reporting_state(tmp_path), NO_EVIDENCE)

    assert reason is not None
    assert reason.startswith("Not recorded: missing evidence (The code lines relied on")
    assert "at least 20 characters" in reason


def test_a_reviewer_recovers_on_the_next_call_once_it_adds_the_field(tmp_path) -> None:
    state = _reporting_state(tmp_path)

    assert _report(state, NO_EVIDENCE) is not None
    assert _report(state, REPORT) is None
    assert _report(state, NO_EVIDENCE) is not None  # the count started again
    assert _report(state, NO_EVIDENCE) is not None
    assert "closed" not in (_report(state, REPORT) or "")


def test_identical_refusals_close_report_finding_to_that_reviewer(tmp_path) -> None:
    state = _reporting_state(tmp_path)

    reasons = [_report(state, NO_EVIDENCE) for _ in range(REPORT_REFUSAL_LIMIT)]

    assert all(r and "missing evidence" in r for r in reasons)
    assert "closed" not in (reasons[-2] or "")
    assert "report_finding is closed to you" in (reasons[-1] or "")
    after = _report(state, REPORT)  # even a good report: the loop is over for this reviewer
    assert after is not None and "report_finding is closed to you" in after
    assert state.collector is not None and state.collector.findings() == []
    assert state.reports_refused == REPORT_REFUSAL_LIMIT + 1
    assert state.closed_reporters() == ["security"]


def test_refusals_are_logged_with_the_reviewer_s_type(tmp_path) -> None:
    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    state = _reporting_state(tmp_path)

    for _ in range(REPORT_REFUSAL_LIMIT):
        _report(state, NO_EVIDENCE)

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    refused = [e for e in events if e["event"] == "report_refused"]
    assert [e["repeats"] for e in refused] == [1, 2, 3]
    assert {e["agent_type"] for e in refused} == {"security"}
    (closed,) = [e for e in events if e["event"] == "report_finding_closed"]
    assert closed["agent_type"] == "security"


def test_one_reviewer_s_loop_does_not_close_the_tool_to_another(tmp_path) -> None:
    state = _reporting_state(tmp_path)
    for _ in range(REPORT_REFUSAL_LIMIT):
        _report(state, NO_EVIDENCE, agent_id="a-sec")

    assert _report(state, REPORT, agent_id="a-cor") is None


def test_a_different_refusal_starts_the_count_again(tmp_path) -> None:
    state = _reporting_state(tmp_path)
    no_title = {**REPORT, "title": "x" * 101}

    for _ in range(REPORT_REFUSAL_LIMIT - 1):
        _report(state, NO_EVIDENCE)
    reason = _report(state, no_title)

    assert reason is not None and "title" in reason and "closed" not in reason


def test_reports_pass_through_where_nothing_records_findings() -> None:
    assert _report(HookState(), NO_EVIDENCE) is None  # the verifier has no report_finding
