"""The ReviewAgent implementation backed by the Claude Agent SDK.

One method per stage: review() runs the lead with its specialists and
returns a Review; verify() runs the verifier on one finding and returns a
Verdict. Both validate the structured output into the domain model and
turn a mismatch into ReviewProtocolError.
"""

from pydantic import ValidationError

from argus.agent.hooks import LEAD_AGENT, HookState
from argus.agent.options import SPECIALISTS, lead_options, verifier_options
from argus.agent.prompts import lead_user_prompt, verifier_user_prompt
from argus.agent.runner import Runner, SdkRunner
from argus.domain.errors import ReviewProtocolError
from argus.domain.models import AgentMetrics, Finding, Review, ReviewContext, Verdict
from argus.pipeline import StageOutcome
from argus.settings import Settings
from argus.telemetry import get_logger


class SdkReviewAgent:
    def __init__(self, settings: Settings, runner: Runner | None = None) -> None:
        self._settings = settings
        self._runner = runner or SdkRunner()

    async def review(self, context: ReviewContext) -> StageOutcome[Review]:
        state = HookState(read_budget=self._settings.lead_read_budget)
        options = lead_options(self._settings, context, state)
        outcome = await self._runner.run(
            lead_user_prompt(context), options, stage="review", state=state
        )
        try:
            review = Review.model_validate(outcome.structured_output)
        except ValidationError as exc:
            raise ReviewProtocolError(
                f"review: output is not a valid Review: {exc}",
                cost_usd=outcome.metrics.cost_usd,
                session_id=outcome.session_id,
            ) from exc
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
