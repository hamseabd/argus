from pathlib import Path

import pytest
from pydantic import ValidationError

from argus.domain.models import (
    AgentMetrics,
    ChangedFile,
    Finding,
    PRInfo,
    Review,
    ReviewContext,
    ReviewResult,
    RunConfig,
    StageMetrics,
    Verdict,
    rank_findings,
)


def finding(**overrides) -> Finding:
    base = {
        "id": "correctness-1",
        "file": "pkg/module.py",
        "line": 10,
        "severity": "high",
        "category": "correctness",
        "title": "Off-by-one in loop bound",
        "description": "The loop stops one element early.",
        "evidence": "range(len(items) - 1) skips the last item.",
        "confidence": 0.9,
    }
    return Finding(**{**base, **overrides})


def test_finding_defaults_to_pending_with_optional_fields_unset() -> None:
    f = finding()

    assert f.status == "pending"
    assert f.end_line is None
    assert f.suggested_fix is None


def test_finding_line_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        finding(line=0)


def test_finding_end_line_cannot_precede_line() -> None:
    with pytest.raises(ValidationError):
        finding(line=10, end_line=9)


def test_finding_title_is_capped_at_100_chars() -> None:
    with pytest.raises(ValidationError):
        finding(title="x" * 101)


def test_finding_confidence_is_a_probability() -> None:
    with pytest.raises(ValidationError):
        finding(confidence=1.5)


def test_finding_rejects_unknown_severity_and_category() -> None:
    with pytest.raises(ValidationError):
        finding(severity="urgent")
    with pytest.raises(ValidationError):
        finding(category="style")


def test_finding_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        finding(extra="nope")


def test_with_status_returns_a_new_finding_and_leaves_the_original() -> None:
    original = finding()

    confirmed = original.with_status("confirmed")

    assert confirmed.status == "confirmed"
    assert original.status == "pending"
    assert confirmed.model_dump(exclude={"status"}) == original.model_dump(exclude={"status"})


def test_location_shows_a_range_only_when_it_spans_lines() -> None:
    assert finding(line=10).location == "pkg/module.py:10"
    assert finding(line=10, end_line=10).location == "pkg/module.py:10"
    assert finding(line=10, end_line=12).location == "pkg/module.py:10-12"


def test_finding_is_immutable() -> None:
    with pytest.raises(ValidationError):
        finding().status = "confirmed"  # type: ignore[misc]


def test_review_assigns_ids_per_category_when_missing() -> None:
    raw = {
        "summary": "Two problems.",
        "files_reviewed": ["a.py"],
        "findings": [
            finding().model_dump(exclude={"id", "status"}),
            finding(category="security").model_dump(exclude={"id", "status"}),
            finding().model_dump(exclude={"id", "status"}),
        ],
    }

    review = Review.model_validate(raw)

    assert [f.id for f in review.findings] == ["correctness-1", "security-1", "correctness-2"]


def test_review_keeps_ids_that_are_present() -> None:
    review = Review(summary="s", files_reviewed=[], findings=[finding(id="custom-7")])

    assert review.findings[0].id == "custom-7"


def test_review_rejects_duplicate_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        Review(summary="s", files_reviewed=[], findings=[finding(), finding()])


def test_review_caps_findings_at_25() -> None:
    many = [finding(id=f"correctness-{i}") for i in range(26)]

    with pytest.raises(ValidationError):
        Review(summary="s", files_reviewed=[], findings=many)


def test_verdict_confidence_is_a_probability() -> None:
    with pytest.raises(ValidationError):
        Verdict(finding_id="x", verdict="confirmed", reasoning="r", confidence=-0.1)
    with pytest.raises(ValidationError):
        Verdict(finding_id="x", verdict="maybe", reasoning="r", confidence=0.5)


def test_stage_metrics_default_the_counters_to_zero() -> None:
    metrics = StageMetrics(
        stage="verify:correctness-1",
        model="claude-sonnet-5",
        cost_usd=0.01,
        input_tokens=10,
        output_tokens=5,
        num_turns=1,
        duration_ms=100,
    )

    assert metrics.subagents_run == 0
    assert metrics.output_rejections == 0
    assert metrics.agents == []


def test_agent_metrics_default_every_counter_to_zero() -> None:
    agent = AgentMetrics(agent="security")

    assert (agent.turns, agent.tool_calls, agent.tool_failures) == (0, 0, 0)
    assert (agent.output_tokens, agent.cache_read_input_tokens, agent.duration_ms) == (0, 0, 0)


def test_review_result_round_trips_through_json() -> None:
    result = ReviewResult(
        review=Review(summary="s", files_reviewed=["a.py"], findings=[finding()]),
        verdicts=[
            Verdict(finding_id="correctness-1", verdict="confirmed", reasoning="r", confidence=0.8)
        ],
        metrics=[],
        total_cost_usd=0.5,
        session_id="abc",
    )

    assert ReviewResult.model_validate_json(result.model_dump_json()) == result


def test_changed_file_status_is_constrained() -> None:
    assert ChangedFile(path="a.py", status="renamed", previous_path="b.py").previous_path == "b.py"
    with pytest.raises(ValidationError):
        ChangedFile(path="a.py", status="moved")


def test_review_context_defaults(tmp_path: Path) -> None:
    ctx = ReviewContext(source="local", repo_root=tmp_path, diff_text="", files=[])

    assert ctx.truncated_files == []
    assert ctx.pr is None


def test_pr_info_fields() -> None:
    pr = PRInfo(
        owner="hamseabd",
        repo="argus",
        number=3,
        title="t",
        body="",
        base_ref="main",
        head_ref="feat/x",
        head_sha="a" * 40,
        html_url="https://github.com/hamseabd/argus/pull/3",
    )

    assert pr.number == 3


def test_rank_drops_rejected_and_orders_by_status_severity_then_path() -> None:
    findings = [
        finding(id="a", file="z.py", severity="low", status="confirmed"),
        finding(id="b", file="m.py", severity="critical", status="rejected"),
        finding(id="c", file="b.py", severity="critical", status="unverified"),
        finding(id="d", file="a.py", severity="high", status="confirmed"),
        finding(id="e", file="c.py", severity="high", status="confirmed"),
        finding(id="f", file="a.py", severity="high", status="confirmed", line=3),
    ]

    ranked = rank_findings(findings)

    assert [f.id for f in ranked] == ["f", "d", "e", "a", "c"]


def test_rank_returns_a_new_list_and_leaves_input_alone() -> None:
    findings = [finding(id="a", status="confirmed"), finding(id="b", status="rejected")]

    ranked = rank_findings(findings)

    assert len(findings) == 2
    assert ranked is not findings


def run_config(**overrides) -> RunConfig:
    base = dict(
        argus_version="0.1.0",
        prompts_sha="0123456789ab",
        lead_model="claude-opus-5",
        lead_effort="high",
        lead_max_turns=40,
        lead_read_budget=10,
        lead_max_budget_usd=3.0,
        lead_max_answer_holds=3,
        specialist_model="claude-sonnet-5",
        specialist_effort="medium",
        specialist_max_turns=25,
        verifier_model="claude-sonnet-5",
        verifier_effort="medium",
        verifier_max_turns=10,
        verifier_max_budget_usd=0.5,
        verify_concurrency=4,
        diff_size_cap=204800,
    )
    return RunConfig(**{**base, **overrides})


def test_run_config_fingerprint_is_filled_in_and_stable() -> None:
    config = run_config()

    assert len(config.fingerprint) == 12
    assert int(config.fingerprint, 16) >= 0
    assert run_config().fingerprint == config.fingerprint


def test_run_config_fingerprint_changes_with_a_prompt_or_a_knob_but_not_the_version() -> None:
    config = run_config()

    assert run_config(prompts_sha="ba9876543210").fingerprint != config.fingerprint
    assert run_config(lead_effort="low").fingerprint != config.fingerprint
    assert run_config(verifier_max_turns=11).fingerprint != config.fingerprint
    assert run_config(argus_version="9.9.9").fingerprint == config.fingerprint


def test_run_config_round_trips_through_its_own_dump() -> None:
    config = run_config()

    restored = RunConfig.model_validate(config.model_dump())

    assert restored == config
    assert restored.fingerprint == config.fingerprint


def test_review_result_carries_an_optional_config() -> None:
    review = Review(summary="s", findings=[], files_reviewed=[])
    without = ReviewResult(review=review, verdicts=[], metrics=[], total_cost_usd=0, session_id="s")
    with_config = without.model_copy(update={"config": run_config()})

    assert without.config is None
    assert with_config.config is not None
    assert with_config.config.lead_model == "claude-opus-5"
