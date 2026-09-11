import asyncio
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from argus.agent.review import SdkReviewAgent
from argus.agent.runner import SdkRunner
from argus.domain.errors import ReviewProtocolError
from argus.domain.models import Finding, Review, ReviewContext
from argus.settings import Settings

RAW_FINDING = {
    "file": "app/repo.py",
    "line": 8,
    "end_line": None,
    "severity": "critical",
    "category": "security",
    "title": "SQL built from user input",
    "description": "d",
    "evidence": "e",
    "suggested_fix": None,
    "confidence": 0.95,
}


def context(tmp_path: Path) -> ReviewContext:
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


def test_review_runs_the_lead_and_returns_a_review_with_ids(tmp_path: Path) -> None:
    recorder = Recorder(
        result({"summary": "s", "files_reviewed": ["app/repo.py"], "findings": [RAW_FINDING]})
    )

    outcome = asyncio.run(agent(recorder).review(context(tmp_path)))

    assert isinstance(outcome.value, Review)
    assert outcome.value.findings[0].id == "security-1"
    assert outcome.metrics.stage == "review"
    assert outcome.session_id == "sess"
    prompt, options = recorder.calls[0]
    assert "app/repo.py" in prompt
    assert options.model == "claude-opus-5"
    assert options.cwd == tmp_path


def test_review_output_that_fails_validation_is_a_protocol_error(tmp_path: Path) -> None:
    recorder = Recorder(result({"summary": "s", "findings": [{"file": "x"}], "files_reviewed": []}))

    with pytest.raises(ReviewProtocolError, match="Review"):
        asyncio.run(agent(recorder).review(context(tmp_path)))


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

    with pytest.raises(ReviewProtocolError, match="Verdict"):
        asyncio.run(agent(recorder).verify(context(tmp_path), finding, ""))


def test_review_warns_when_fewer_than_three_specialists_ran(tmp_path: Path) -> None:
    import io
    import json

    from argus import telemetry

    stream = io.StringIO()
    telemetry.configure(log_format="json", stream=stream)
    recorder = Recorder(result({"summary": "s", "files_reviewed": [], "findings": []}))

    asyncio.run(agent(recorder).review(context(tmp_path)))

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    warning = next(e for e in events if e["event"] == "specialists_missing")
    assert warning["expected"] == 3
    assert warning["ran"] == 0
