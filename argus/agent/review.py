"""The ReviewAgent implementation backed by the Claude Agent SDK.

One method per stage: review() runs the lead with its specialists and
returns a Review; verify() runs the verifier on one finding and returns a
Verdict. Both validate the structured output into the domain model and
turn a mismatch into ReviewProtocolError.
"""

from pydantic import ValidationError

from argus.agent.hooks import HookState
from argus.agent.options import SPECIALISTS, lead_options, verifier_options
from argus.agent.prompts import lead_user_prompt, verifier_user_prompt
from argus.agent.runner import Runner, SdkRunner
from argus.domain.errors import ReviewProtocolError
from argus.domain.models import Finding, Review, ReviewContext, Verdict
from argus.pipeline import StageOutcome
from argus.settings import Settings
from argus.telemetry import get_logger


class SdkReviewAgent:
    def __init__(self, settings: Settings, runner: Runner | None = None) -> None:
        self._settings = settings
        self._runner = runner or SdkRunner()

    async def review(self, context: ReviewContext) -> StageOutcome[Review]:
        state = HookState()
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
