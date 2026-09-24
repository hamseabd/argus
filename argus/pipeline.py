"""Stage orchestration: review, verify, then fold into a ReviewResult.

Python owns the pipeline; the agent behind the ReviewAgent protocol owns
the queries. The protocol is defined here, by its consumer, and it speaks
only domain types, so the whole flow is testable with a fake agent and
nothing in this module knows the SDK exists.
"""

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from argus.context.diff import diff_sections
from argus.domain.errors import AgentRunError, ArgusError, ReviewProtocolError
from argus.domain.models import (
    Finding,
    Review,
    ReviewContext,
    ReviewResult,
    StageMetrics,
    Status,
    Verdict,
)
from argus.telemetry import bind_run, get_logger, new_run_id
from argus.tracing import meta, record_error, span, stage_attributes

DEFAULT_VERIFY_CONCURRENCY = 4
PAID_ERRORS = (AgentRunError, ReviewProtocolError)
"""Errors raised after a query was billed; they carry cost_usd."""


@dataclass(frozen=True)
class StageOutcome[T]:
    value: T
    metrics: StageMetrics
    session_id: str


@dataclass(frozen=True)
class VerifyFailure:
    """A verification that did not produce a verdict, and what it cost anyway."""

    cost_usd: float


class ReviewAgent(Protocol):
    async def review(self, context: ReviewContext) -> StageOutcome[Review]: ...

    async def verify(
        self, context: ReviewContext, finding: Finding, diff_section: str
    ) -> StageOutcome[Verdict]: ...


async def run_review(
    context: ReviewContext,
    agent: ReviewAgent,
    *,
    verify: bool = True,
    verify_concurrency: int = DEFAULT_VERIFY_CONCURRENCY,
    run_id: str | None = None,
) -> ReviewResult:
    """Run the review stage, verify each finding, and return everything produced.

    Pass run_id when the caller already bound one for its own log events;
    contextvars bound inside this coroutine do not reach the caller's context.
    The whole run is one trace: argus.run, with a span per stage under it.
    """
    run_id = run_id or new_run_id()
    attributes = meta(
        run_id=run_id,
        source=context.source,
        pr=context.pr.number if context.pr else None,
        head_sha=context.pr.head_sha if context.pr else None,
        verify=verify,
    )
    with span("argus.run", "chain", attributes=attributes) as run_span:
        result = await _run_review(context, agent, verify, verify_concurrency, run_id)
        findings = result.review.findings
        run_span.set_attributes(
            meta(
                total_cost_usd=result.total_cost_usd,
                confirmed=sum(f.status == "confirmed" for f in findings),
                unverified=sum(f.status == "unverified" for f in findings),
                rejected=sum(f.status == "rejected" for f in findings),
            )
        )
        return result


async def _run_review(
    context: ReviewContext,
    agent: ReviewAgent,
    verify: bool,
    verify_concurrency: int,
    run_id: str,
) -> ReviewResult:
    log = get_logger()
    started = time.monotonic()
    bind_run(run_id=run_id)
    log.info(
        "run_start",
        source=context.source,
        files=len(context.files),
        truncated_files=len(context.truncated_files),
        pr=context.pr.number if context.pr else None,
        verify=verify,
    )
    with span("argus.review", "chain") as stage:
        reviewed = await agent.review(context)
        stage.set_attributes(stage_attributes(reviewed.metrics))
    review = reviewed.value
    metrics = [reviewed.metrics]
    verdicts: list[Verdict] = []
    statuses: dict[str, Status] = {f.id: "unverified" for f in review.findings}
    failed_cost = 0.0

    if verify and review.findings:
        outcomes = await _verify_all(context, agent, review.findings, verify_concurrency)
        for finding, outcome in zip(review.findings, outcomes, strict=True):
            if isinstance(outcome, VerifyFailure):
                failed_cost += outcome.cost_usd
                continue
            verdicts.append(outcome.value)
            metrics.append(outcome.metrics)
            statuses[finding.id] = outcome.value.verdict

    findings = [f.with_status(statuses[f.id]) for f in review.findings]
    total_cost = round(sum(m.cost_usd for m in metrics) + failed_cost, 6)
    duration_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "run_end",
        total_cost_usd=total_cost,
        confirmed=sum(f.status == "confirmed" for f in findings),
        unverified=sum(f.status == "unverified" for f in findings),
        rejected=sum(f.status == "rejected" for f in findings),
    )
    return ReviewResult(
        review=review.model_copy(update={"findings": findings}),
        verdicts=verdicts,
        metrics=metrics,
        total_cost_usd=total_cost,
        duration_ms=duration_ms,
        session_id=reviewed.session_id,
    )


async def _verify_all(
    context: ReviewContext,
    agent: ReviewAgent,
    findings: Sequence[Finding],
    concurrency: int,
) -> list[StageOutcome[Verdict] | VerifyFailure]:
    """One verifier query per finding, at most `concurrency` at a time.

    A failed verification never aborts the run: the finding stays unverified,
    the failure is logged, and what the failed query cost is returned in
    place of an outcome so the run total stays honest.
    """
    sections = diff_sections(context.diff_text)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(finding: Finding) -> StageOutcome[Verdict] | VerifyFailure:
        async with semaphore:
            attributes = meta(
                finding_id=finding.id,
                severity=finding.severity,
                category=finding.category,
                title=finding.title,
            )
            with span(
                "argus.verify", "chain", display=f"argus.verify {finding.id}", attributes=attributes
            ) as stage:
                try:
                    outcome = await agent.verify(context, finding, sections.get(finding.file, ""))
                except ArgusError as exc:
                    record_error(stage, exc)
                    cost = float(exc.cost_usd) if isinstance(exc, PAID_ERRORS) else 0.0
                    get_logger().warning(
                        "verify_failed", finding=finding.id, error=str(exc), cost_usd=cost
                    )
                    return VerifyFailure(cost)
                stage.set_attributes(
                    {
                        **stage_attributes(outcome.metrics),
                        **meta(verdict=outcome.value.verdict, confidence=outcome.value.confidence),
                    }
                )
                return outcome

    return await asyncio.gather(*(one(f) for f in findings))
