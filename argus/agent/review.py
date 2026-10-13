"""The ReviewAgent implementation backed by the Claude Agent SDK.

One method per stage: review() runs the lead with its specialists and
returns a Review; verify() runs the verifier on one finding and returns a
Verdict. Both validate the structured output into the domain model and
turn a mismatch into ReviewProtocolError.

The review is assembled here, not written by the lead: the specialists
record findings through report_finding, the lead answers with a Triage that
names the ones to drop, and what remains becomes the Review.
"""

from pydantic import ValidationError

from argus.agent.findings import FindingCollector
from argus.agent.hooks import LEAD_AGENT, HookState
from argus.agent.options import SPECIALISTS, lead_options, verifier_options
from argus.agent.prompts import lead_user_prompt, verifier_user_prompt
from argus.agent.runner import Runner, SdkRunner
from argus.domain.errors import ReviewProtocolError
from argus.domain.models import (
    MAX_FINDINGS,
    SEVERITY_ORDER,
    AgentMetrics,
    Finding,
    Review,
    ReviewContext,
    Triage,
    Verdict,
)
from argus.pipeline import StageOutcome
from argus.settings import Settings
from argus.telemetry import get_logger


class SdkReviewAgent:
    def __init__(self, settings: Settings, runner: Runner | None = None) -> None:
        self._settings = settings
        self._runner = runner or SdkRunner(trace_content=settings.trace_content)

    async def review(self, context: ReviewContext) -> StageOutcome[Review]:
        collector = FindingCollector(context.repo_root)
        state = HookState(
            read_budget=self._settings.lead_read_budget,
            answer_hold_limit=self._settings.lead_max_answer_holds,
            collector=collector,
        )
        options = lead_options(self._settings, context, state)
        outcome = await self._runner.run(
            lead_user_prompt(context), options, stage="review", state=state
        )
        try:
            triage = Triage.model_validate(outcome.structured_output)
        except ValidationError as exc:
            raise ReviewProtocolError(
                f"review: output is not a valid Triage: {exc}",
                cost_usd=outcome.metrics.cost_usd,
                session_id=outcome.session_id,
            ) from exc
        review = assemble_review(triage, collector.findings())
        if state.subagents_started < len(SPECIALISTS):
            get_logger().warning(
                "specialists_missing", expected=len(SPECIALISTS), ran=state.subagents_started
            )
        capped = turn_capped_specialists(
            outcome.metrics.agents, self._settings.specialist_max_turns
        )
        if capped:
            get_logger().warning(
                "specialist_turn_cap", agents=capped, max_turns=self._settings.specialist_max_turns
            )
        if rerun := rerun_specialists(outcome.metrics.agents):
            get_logger().warning("specialist_rerun", agents=rerun)
        return StageOutcome(review, outcome.metrics, outcome.session_id)

    async def verify(
        self, context: ReviewContext, finding: Finding, diff_section: str
    ) -> StageOutcome[Verdict]:
        state = HookState()
        options = verifier_options(self._settings, context, state)
        stage = f"verify:{finding.id}"
        outcome = await self._runner.run(
            verifier_user_prompt(finding, diff_section), options, stage=stage, state=state
        )
        payload = outcome.structured_output
        cost, session = outcome.metrics.cost_usd, outcome.session_id
        if not isinstance(payload, dict):
            raise ReviewProtocolError(
                f"{stage}: output is not a valid Verdict: not an object", cost, session
            )
        try:
            verdict = Verdict.model_validate({**payload, "finding_id": finding.id})
        except ValidationError as exc:
            raise ReviewProtocolError(
                f"{stage}: output is not a valid Verdict: {exc}", cost, session
            ) from exc
        return StageOutcome(verdict, outcome.metrics, outcome.session_id)


def turn_capped_specialists(agents: list[AgentMetrics], max_turns: int) -> list[str]:
    """Specialists that used every turn they had, so their findings may be incomplete.

    The cap is per run, and a rerun specialist's turns are summed, so those are
    left to rerun_specialists rather than misreported here.
    """
    return [
        a.agent for a in agents if a.agent != LEAD_AGENT and a.runs == 1 and a.turns >= max_turns
    ]


def rerun_specialists(agents: list[AgentMetrics]) -> list[str]:
    """Specialists the lead started more than once, against its instructions."""
    return [a.agent for a in agents if a.agent != LEAD_AGENT and a.runs > 1]


def assemble_review(triage: Triage, recorded: list[Finding]) -> Review:
    """The Review: every recorded finding the lead did not drop, numbered per category.

    A drop naming an id that was never recorded is logged and ignored. Past
    MAX_FINDINGS, the most severe and most confident are kept, so a noisy run
    loses its lows, never its criticals.
    """
    log = get_logger()
    ids = {f.id for f in recorded}
    if unknown := sorted(triage.dropped_ids - ids):
        log.warning("unknown_drops", ids=unknown)
    kept = [f for f in recorded if f.id not in triage.dropped_ids]
    log.info(
        "findings_triaged",
        recorded=len(recorded),
        dropped=len(recorded) - len(kept),
        reasons={d.id: d.reason for d in triage.dropped if d.id in ids},
    )
    if len(kept) > MAX_FINDINGS:
        log.warning("findings_capped", kept=MAX_FINDINGS, recorded=len(kept))
        ranked = sorted(kept, key=lambda f: (SEVERITY_ORDER[f.severity], -f.confidence))
        top = {f.id for f in ranked[:MAX_FINDINGS]}
        kept = [f for f in kept if f.id in top]
    # Recorded ids (F1, F2, ...) are the lead's handles; the review numbers findings per
    # category, as before, for the verifier, the report, and the known-finding match.
    return Review.model_validate(
        {
            "summary": triage.summary,
            "files_reviewed": triage.files_reviewed,
            "findings": [f.model_dump(exclude={"id", "status"}) for f in kept],
        }
    )
