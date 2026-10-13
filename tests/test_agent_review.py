import asyncio
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from argus.agent.hooks import HookState
from argus.agent.review import SdkReviewAgent, rerun_specialists, turn_capped_specialists
from argus.agent.runner import SdkRunner
from argus.domain.errors import ReviewProtocolError
from argus.domain.models import AgentMetrics, Finding, Review, ReviewContext
from argus.settings import Settings

RAW_FINDING = {
    "file": "app/repo.py",
    "line": 8,
    "end_line": None,
    "severity": "critical",
    "category": "security",
    "title": "SQL built from user input",
    "description": "The name is interpolated into the SQL text.",
    "evidence": "search() passes the request's name straight into execute().",
    "suggested_fix": None,
    "confidence": 0.95,
}
TRIAGE = {
    "summary": "Adds a search endpoint; one injection finding.",
    "files_reviewed": ["app/repo.py"],
    "dropped": [],
}


def context(tmp_path: Path) -> ReviewContext:
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "repo.py").write_text("".join(f"line {n}\n" for n in range(1, 41)))
    return ReviewContext(
        source="local",
        repo_root=tmp_path,
        diff_text=(
            "diff --git a/app/repo.py b/app/repo.py\n--- a/app/repo.py\n+++ b/app/repo.py\n"
            "@@ -1 +1 @@\n+x\n"
        ),
        files=[],
    )


def result(structured: Any, **overrides: Any) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=2,
        session_id="sess",
        total_cost_usd=0.2,
        usage={"input_tokens": 1, "output_tokens": 2},
        structured_output=structured,
    )
    return ResultMessage(**{**base, **overrides})


class Recorder:
    """A query function that records what it was asked and replays canned results."""

    def __init__(self, *results: ResultMessage) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, ClaudeAgentOptions]] = []

    async def __call__(self, *, prompt: str, options: ClaudeAgentOptions):
        self.calls.append((prompt, options))
        yield self.results.pop(0)


def agent(recorder: Recorder) -> SdkReviewAgent:
    return SdkReviewAgent(Settings(_env_file=None), runner=SdkRunner(query_fn=recorder))


class Reporting:
    """A runner that records findings during the review, as the specialists' tool calls would."""

    def __init__(self, inner: SdkRunner, *reports: tuple[str, dict[str, Any]]) -> None:
        self.inner = inner
        self.reports = reports

    async def run(self, prompt: str, options: ClaudeAgentOptions, *, stage: str, state: HookState):
        assert state.collector is not None
        for reporter, raw in self.reports:
            finding_id = state.collector.add(raw)
            state.collector.attribute(finding_id, reporter)
        return await self.inner.run(prompt, options, stage=stage, state=state)


def reviewer(recorder: Recorder, *reports: tuple[str, dict[str, Any]], **settings: Any):
    runner = Reporting(SdkRunner(query_fn=recorder), *reports)
    return SdkReviewAgent(Settings(_env_file=None, **settings), runner=runner)


def test_review_builds_the_review_from_recorded_findings_with_ids(tmp_path: Path) -> None:
    recorder = Recorder(result(TRIAGE))

    outcome = asyncio.run(reviewer(recorder, ("security", RAW_FINDING)).review(context(tmp_path)))

    assert isinstance(outcome.value, Review)
    (finding,) = outcome.value.findings
    assert (finding.id, finding.reported_by, finding.title) == (
        "security-1",
        "security",
        RAW_FINDING["title"],
    )
    assert outcome.value.summary == TRIAGE["summary"]
    assert outcome.value.files_reviewed == ["app/repo.py"]
    assert outcome.metrics.stage == "review"
    assert outcome.session_id == "sess"
    prompt, options = recorder.calls[0]
    assert "app/repo.py" in prompt
    assert options.model == "claude-opus-5"
    assert options.cwd == tmp_path


def test_findings_the_lead_drops_never_reach_the_review(tmp_path: Path) -> None:
    recorder = Recorder(result({**TRIAGE, "dropped": [{"id": "F2", "reason": "duplicate of F1"}]}))
    duplicate = {**RAW_FINDING, "line": 9}

    outcome = asyncio.run(
        reviewer(recorder, ("security", RAW_FINDING), ("correctness", duplicate)).review(
            context(tmp_path)
        )
    )

    assert [(f.line, f.reported_by) for f in outcome.value.findings] == [(8, "security")]


def test_a_dropped_id_that_was_never_recorded_is_logged_and_ignored(tmp_path: Path) -> None:
    import io
    import json

    from argus import telemetry

    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    recorder = Recorder(result({**TRIAGE, "dropped": [{"id": "F9", "reason": "dup"}]}))

    outcome = asyncio.run(reviewer(recorder, ("security", RAW_FINDING)).review(context(tmp_path)))

    assert len(outcome.value.findings) == 1
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert next(e for e in events if e["event"] == "unknown_drops")["ids"] == ["F9"]


def test_past_the_review_limit_the_most_severe_findings_are_kept(tmp_path: Path) -> None:
    recorder = Recorder(result(TRIAGE))
    lows = [("quality", {**RAW_FINDING, "severity": "low", "line": n}) for n in range(1, 30)]
    critical = ("security", {**RAW_FINDING, "severity": "critical", "line": 30})

    outcome = asyncio.run(reviewer(recorder, *lows, critical).review(context(tmp_path)))

    findings = outcome.value.findings
    assert len(findings) == 25
    assert "critical" in {f.severity for f in findings}


def test_review_output_that_fails_validation_is_a_protocol_error(tmp_path: Path) -> None:
    recorder = Recorder(result({"summary": "s", "findings": [], "files_reviewed": []}))

    with pytest.raises(ReviewProtocolError, match="Triage"):
        asyncio.run(reviewer(recorder).review(context(tmp_path)))


def test_verify_runs_the_verifier_with_the_finding_and_fills_the_id(tmp_path: Path) -> None:
    recorder = Recorder(result({"verdict": "confirmed", "reasoning": "r", "confidence": 0.7}))
    finding = Finding(id="security-1", status="pending", **RAW_FINDING)

    outcome = asyncio.run(
        agent(recorder).verify(context(tmp_path), finding, "diff --git a/app/repo.py\n+x\n")
    )

    assert outcome.value.finding_id == "security-1"
    assert outcome.value.verdict == "confirmed"
    assert outcome.metrics.stage == "verify:security-1"
    prompt, options = recorder.calls[0]
    assert "SQL built from user input" in prompt
    assert "```diff" in prompt
    assert options.model == "claude-sonnet-5"
    assert options.agents is None


def test_verify_output_that_fails_validation_is_a_protocol_error(tmp_path: Path) -> None:
    recorder = Recorder(result({"verdict": "maybe", "reasoning": "r", "confidence": 0.7}))
    finding = Finding(id="security-1", **RAW_FINDING)

    with pytest.raises(ReviewProtocolError, match="Verdict") as info:
        asyncio.run(agent(recorder).verify(context(tmp_path), finding, ""))

    assert info.value.cost_usd == 0.2  # the query was billed even though its answer was unusable


def test_specialists_that_hit_the_turn_cap_are_named() -> None:
    agents = [
        AgentMetrics(agent="lead", turns=40),  # the lead has its own cap
        AgentMetrics(agent="security", turns=14),
        AgentMetrics(agent="quality", turns=15),
        AgentMetrics(agent="correctness", turns=16),
    ]

    assert turn_capped_specialists(agents, max_turns=15) == ["quality", "correctness"]


def test_a_specialist_run_twice_is_reported_as_rerun_not_as_capped() -> None:
    agents = [
        AgentMetrics(agent="lead", turns=3),
        AgentMetrics(agent="security", turns=20, runs=2),  # two runs of 10; the cap is per run
    ]

    assert turn_capped_specialists(agents, max_turns=15) == []
    assert rerun_specialists(agents) == ["security"]


class StateSpy:
    """A runner that records the HookState each stage was given."""

    def __init__(self, inner: SdkRunner) -> None:
        self.inner = inner
        self.states: list[HookState] = []

    async def run(self, prompt: str, options: ClaudeAgentOptions, *, stage: str, state: HookState):
        self.states.append(state)
        return await self.inner.run(prompt, options, stage=stage, state=state)


def test_the_lead_runs_with_a_reading_budget_and_the_verifier_without_one(tmp_path: Path) -> None:
    recorder = Recorder(
        result(TRIAGE),
        result({"verdict": "confirmed", "reasoning": "r", "confidence": 0.7}),
    )
    spy = StateSpy(SdkRunner(query_fn=recorder))
    reviewer = SdkReviewAgent(Settings(_env_file=None, lead_read_budget=7), runner=spy)

    asyncio.run(reviewer.review(context(tmp_path)))
    asyncio.run(reviewer.verify(context(tmp_path), Finding(id="security-1", **RAW_FINDING), ""))

    assert spy.states[0].read_budget == 7
    assert spy.states[1].read_budget is None  # the verifier reads as much as it needs


def test_review_warns_when_fewer_than_three_specialists_ran(tmp_path: Path) -> None:
    import io
    import json

    from argus import telemetry

    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    recorder = Recorder(result(TRIAGE))

    asyncio.run(agent(recorder).review(context(tmp_path)))

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    warning = next(e for e in events if e["event"] == "specialists_missing")
    assert warning["expected"] == 3
    assert warning["ran"] == 0


def test_the_answer_hold_limit_setting_reaches_the_lead_s_hooks(tmp_path: Path) -> None:
    decisions: list[bool] = []

    async def query(*, prompt: str, options: ClaudeAgentOptions):
        ctx = {"signal": None}
        start = {"hook_event_name": "SubagentStart", "agent_id": "a-1", "agent_type": "security"}
        await options.hooks["SubagentStart"][0].hooks[0](start, None, ctx)
        answer = {"hook_event_name": "PreToolUse", "tool_name": "StructuredOutput"}
        hold = next(m for m in options.hooks["PreToolUse"] if m.matcher == "StructuredOutput")
        for _ in range(3):
            decisions.append(bool(await hold.hooks[0](answer, None, ctx)))
        yield result(TRIAGE)

    settings = Settings(_env_file=None, lead_max_answer_holds=1)
    outcome = asyncio.run(
        SdkReviewAgent(settings, runner=SdkRunner(query_fn=query)).review(context(tmp_path))
    )

    assert decisions == [True, False, False]
    assert outcome.metrics.answers_held == 1
