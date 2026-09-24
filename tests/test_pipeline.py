import asyncio
import io
import json
from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.trace import StatusCode

from argus import telemetry
from argus.domain.errors import AgentRunError, ReviewProtocolError
from argus.domain.models import Finding, Review, ReviewContext, StageMetrics, Verdict
from argus.pipeline import StageOutcome, run_review


def metrics(stage: str, cost: float = 0.1) -> StageMetrics:
    return StageMetrics(
        stage=stage,
        model="m",
        cost_usd=cost,
        input_tokens=1,
        output_tokens=1,
        num_turns=1,
        duration_ms=1,
        subagents_run=3 if stage == "review" else 0,
    )


def finding(i: int, **overrides) -> Finding:
    base = dict(
        id=f"correctness-{i}",
        file=f"f{i}.py",
        line=i,
        severity="high",
        category="correctness",
        title=f"t{i}",
        description="d",
        evidence="e",
        confidence=0.5,
    )
    return Finding(**{**base, **overrides})


class FakeAgent:
    def __init__(self, review: Review, verdicts: dict[str, str | Exception]) -> None:
        self._review = review
        self._verdicts = verdicts
        self.verify_calls: list[tuple[str, str]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def review(self, context: ReviewContext) -> StageOutcome[Review]:
        return StageOutcome(self._review, metrics("review", 1.0), "sess")

    async def verify(self, context: ReviewContext, finding: Finding, diff_section: str):
        self.verify_calls.append((finding.id, diff_section))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        outcome = self._verdicts[finding.id]
        if isinstance(outcome, Exception):
            raise outcome
        verdict = Verdict(finding_id=finding.id, verdict=outcome, reasoning="r", confidence=0.8)
        return StageOutcome(verdict, metrics(f"verify:{finding.id}"), "sess")


def context(tmp_path: Path) -> ReviewContext:
    return ReviewContext(
        source="local",
        repo_root=tmp_path,
        files=[],
        diff_text="diff --git a/f1.py b/f1.py\n--- a/f1.py\n+++ b/f1.py\n@@ -1 +1 @@\n+x\n",
    )


def test_full_flow_assigns_final_statuses_and_totals(tmp_path: Path) -> None:
    review = Review(
        summary="s", files_reviewed=["f1.py"], findings=[finding(1), finding(2), finding(3)]
    )
    agent = FakeAgent(
        review,
        {
            "correctness-1": "confirmed",
            "correctness-2": "rejected",
            "correctness-3": AgentRunError("error_max_turns", 0.05),
        },
    )

    result = asyncio.run(run_review(context(tmp_path), agent))

    statuses = {f.id: f.status for f in result.review.findings}
    assert statuses == {
        "correctness-1": "confirmed",
        "correctness-2": "rejected",
        "correctness-3": "unverified",
    }
    assert [v.finding_id for v in result.verdicts] == ["correctness-1", "correctness-2"]
    assert [m.stage for m in result.metrics] == [
        "review",
        "verify:correctness-1",
        "verify:correctness-2",
    ]
    assert result.total_cost_usd == 1.25  # the failed verification's $0.05 counts too
    assert result.duration_ms > 0
    assert result.session_id == "sess"
    assert result.review.summary == "s"


def test_verify_gets_the_finding_file_section_of_the_diff(tmp_path: Path) -> None:
    review = Review(summary="s", files_reviewed=[], findings=[finding(1), finding(2)])
    agent = FakeAgent(review, {"correctness-1": "confirmed", "correctness-2": "confirmed"})

    asyncio.run(run_review(context(tmp_path), agent))

    sections = dict(agent.verify_calls)
    assert sections["correctness-1"].startswith("diff --git a/f1.py")
    assert sections["correctness-2"] == ""


def test_no_verify_marks_everything_unverified_without_calling_the_verifier(tmp_path: Path) -> None:
    review = Review(summary="s", files_reviewed=[], findings=[finding(1)])
    agent = FakeAgent(review, {})

    result = asyncio.run(run_review(context(tmp_path), agent, verify=False))

    assert agent.verify_calls == []
    assert result.verdicts == []
    assert result.review.findings[0].status == "unverified"
    assert result.total_cost_usd == 1.0


def test_verification_is_bounded_by_the_concurrency_limit(tmp_path: Path) -> None:
    findings = [finding(i) for i in range(1, 9)]
    review = Review(summary="s", files_reviewed=[], findings=findings)
    agent = FakeAgent(review, {f.id: "confirmed" for f in findings})

    asyncio.run(run_review(context(tmp_path), agent, verify_concurrency=3))

    assert agent.max_in_flight == 3
    assert len(agent.verify_calls) == 8


def test_empty_review_skips_verification(tmp_path: Path) -> None:
    agent = FakeAgent(Review(summary="clean", files_reviewed=["f1.py"], findings=[]), {})

    result = asyncio.run(run_review(context(tmp_path), agent))

    assert result.review.findings == []
    assert result.total_cost_usd == 1.0


def test_run_events_are_logged_with_a_run_id(tmp_path: Path) -> None:
    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    review = Review(summary="s", files_reviewed=[], findings=[finding(1), finding(2)])
    agent = FakeAgent(
        review, {"correctness-1": "confirmed", "correctness-2": AgentRunError("x", 0.0)}
    )

    asyncio.run(run_review(context(tmp_path), agent))

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    names = [e["event"] for e in events]
    assert names[0] == "run_start"
    assert names[-1] == "run_end"
    assert "verify_failed" in names
    assert len({e["run_id"] for e in events}) == 1
    end = events[-1]
    assert end["confirmed"] == 1
    assert end["unverified"] == 1
    assert end["rejected"] == 0
    assert end["total_cost_usd"] == 1.1


def test_a_caller_supplied_run_id_is_used(tmp_path: Path) -> None:
    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    agent = FakeAgent(Review(summary="s", files_reviewed=[], findings=[]), {})

    asyncio.run(run_review(context(tmp_path), agent, run_id="given-1"))

    assert {json.loads(line)["run_id"] for line in stream.getvalue().splitlines()} == {"given-1"}


def test_a_paid_but_invalid_verdict_still_counts_toward_the_total(tmp_path: Path) -> None:
    review = Review(summary="s", files_reviewed=[], findings=[finding(1)])
    agent = FakeAgent(review, {"correctness-1": ReviewProtocolError("bad verdict", cost_usd=0.2)})

    result = asyncio.run(run_review(context(tmp_path), agent))

    assert result.review.findings[0].status == "unverified"
    assert result.total_cost_usd == 1.2


def test_an_integer_cost_on_a_failed_verification_is_handled(tmp_path: Path) -> None:
    review = Review(summary="s", files_reviewed=[], findings=[finding(1)])
    agent = FakeAgent(review, {"correctness-1": AgentRunError("error_max_turns", 0)})

    result = asyncio.run(run_review(context(tmp_path), agent))

    assert result.total_cost_usd == 1.0


def by_name(finished, name: str) -> list:
    return [s for s in finished if s.name == name]


def test_a_run_is_one_trace_with_a_review_and_a_span_per_verification(
    tmp_path: Path, spans
) -> None:
    review = Review(summary="s", files_reviewed=["f1.py"], findings=[finding(1), finding(2)])
    agent = FakeAgent(
        review,
        {
            "correctness-1": "confirmed",
            "correctness-2": ReviewProtocolError("no verdict", cost_usd=0.05),
        },
    )

    asyncio.run(run_review(context(tmp_path), agent, run_id="r1"))

    finished = spans.get_finished_spans()
    (run,) = by_name(finished, "argus.run")
    (rev,) = by_name(finished, "argus.review")
    verifies = by_name(finished, "argus.verify")
    assert {s.context.trace_id for s in finished} == {run.context.trace_id}
    assert run.parent is None
    assert rev.parent.span_id == run.context.span_id
    assert len(verifies) == 2
    assert all(v.parent.span_id == run.context.span_id for v in verifies)

    assert run.attributes["langsmith.metadata.run_id"] == "r1"
    assert run.attributes["langsmith.metadata.source"] == "local"
    assert run.attributes["langsmith.metadata.total_cost_usd"] == 1.15
    assert run.attributes["langsmith.metadata.confirmed"] == 1
    assert run.attributes["langsmith.metadata.unverified"] == 1
    assert rev.attributes["langsmith.metadata.cost_usd"] == 1.0

    ok = next(
        v for v in verifies if v.attributes["langsmith.metadata.finding_id"] == "correctness-1"
    )
    assert ok.attributes["langsmith.trace.name"] == "argus.verify correctness-1"
    assert ok.attributes["langsmith.metadata.verdict"] == "confirmed"
    assert ok.attributes["langsmith.metadata.severity"] == "high"
    assert ok.attributes["langsmith.metadata.title"] == "t1"
    failed = next(
        v for v in verifies if v.attributes["langsmith.metadata.finding_id"] == "correctness-2"
    )
    assert failed.status.status_code == StatusCode.ERROR
    assert failed.attributes["langsmith.metadata.cost_usd"] == 0.05


def test_each_verification_runs_inside_its_own_span(tmp_path: Path, spans) -> None:
    seen: dict[str, int] = {}

    class SpanSpy(FakeAgent):
        async def verify(self, context, finding, diff_section):
            seen[finding.id] = otel_trace.get_current_span().get_span_context().span_id
            return await super().verify(context, finding, diff_section)

    review = Review(summary="s", files_reviewed=[], findings=[finding(1), finding(2), finding(3)])
    agent = SpanSpy(review, {f"correctness-{i}": "confirmed" for i in (1, 2, 3)})

    asyncio.run(run_review(context(tmp_path), agent, verify_concurrency=3))

    verifies = {
        s.attributes["langsmith.metadata.finding_id"]: s.context.span_id
        for s in by_name(spans.get_finished_spans(), "argus.verify")
    }
    assert seen == verifies


def test_a_failed_review_stage_marks_the_run_and_the_stage(tmp_path: Path, spans) -> None:
    class Broken(FakeAgent):
        async def review(self, context):
            raise AgentRunError("error_max_budget_usd", 3.0)

    with pytest.raises(AgentRunError):
        asyncio.run(
            run_review(
                context(tmp_path), Broken(Review(summary="s", files_reviewed=[], findings=[]), {})
            )
        )

    finished = spans.get_finished_spans()
    (run,) = by_name(finished, "argus.run")
    (rev,) = by_name(finished, "argus.review")
    assert rev.status.status_code == StatusCode.ERROR
    assert rev.attributes["langsmith.metadata.cost_usd"] == 3.0
    assert run.status.status_code == StatusCode.ERROR
