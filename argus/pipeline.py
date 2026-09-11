"""Stage orchestration: review, verify, then fold into a ReviewResult.

Python owns the pipeline; the agent behind the ReviewAgent protocol owns
the queries. The protocol is defined here, by its consumer, and it speaks
only domain types, so the whole flow is testable with a fake agent and
nothing in this module knows the SDK exists.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from argus.context.diff import diff_sections
from argus.domain.errors import ArgusError
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

DEFAULT_VERIFY_CONCURRENCY = 4


@dataclass(frozen=True)
class StageOutcome[T]:
    value: T
    metrics: StageMetrics
    session_id: str


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
) -> ReviewResult:
    """Run the review stage, verify each finding, and return everything produced."""
    log = get_logger()
    bind_run(run_id=new_run_id())
    log.info(
        "run_start",
        source=context.source,
        files=len(context.files),
        truncated_files=len(context.truncated_files),
        pr=context.pr.number if context.pr else None,
        verify=verify,
    )
    reviewed = await agent.review(context)
    review = reviewed.value
    metrics = [reviewed.metrics]
    verdicts: list[Verdict] = []
    statuses: dict[str, Status] = {f.id: "unverified" for f in review.findings}

    if verify and review.findings:
        outcomes = await _verify_all(context, agent, review.findings, verify_concurrency)
        for finding, outcome in zip(review.findings, outcomes, strict=True):
            if outcome is None:
                continue
            verdicts.append(outcome.value)
            metrics.append(outcome.metrics)
            statuses[finding.id] = outcome.value.verdict

    findings = [f.with_status(statuses[f.id]) for f in review.findings]
    total_cost = round(sum(m.cost_usd for m in metrics), 6)
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
        session_id=reviewed.session_id,
    )


async def _verify_all(
    context: ReviewContext,
    agent: ReviewAgent,
    findings: Sequence[Finding],
    concurrency: int,
) -> list[StageOutcome[Verdict] | None]:
    """One verifier query per finding, at most `concurrency` at a time.

    A failed verification never aborts the run: the finding stays unverified
    and the failure is logged.
    """
    sections = diff_sections(context.diff_text)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(finding: Finding) -> StageOutcome[Verdict] | None:
        async with semaphore:
            try:
                return await agent.verify(context, finding, sections.get(finding.file, ""))
            except ArgusError as exc:
                get_logger().warning("verify_failed", finding=finding.id, error=str(exc))
                return None

    return await asyncio.gather(*(one(f) for f in findings))
