"""The merge gate as a check run: one annotation per finding, a conclusion from --fail-on."""

from pathlib import Path

from argus.domain.models import (
    MAX_FINDINGS,
    ChangedFile,
    Finding,
    PRInfo,
    Review,
    ReviewContext,
    ReviewResult,
    StageMetrics,
)
from argus.report.check_run import CHECK_NAME, MAX_ANNOTATIONS, build_check_run


def context() -> ReviewContext:
    return ReviewContext(
        source="pr",
        repo_root=Path("/repo"),
        diff_text="",
        files=[ChangedFile(path="a.py", status="modified")],
        pr=PRInfo(
            owner="o",
            repo="r",
            number=7,
            title="t",
            body="",
            base_ref="main",
            head_ref="f",
            head_sha="a" * 40,
            html_url="https://github.com/o/r/pull/7",
        ),
    )


def finding(i: int, severity: str = "high", status: str = "confirmed", **extra) -> Finding:
    return Finding(
        id=f"correctness-{i}",
        file="a.py",
        line=i,
        severity=severity,
        category="correctness",
        title=f"Problem {i}",
        description="What is wrong.",
        evidence="Why it is real.",
        confidence=0.8,
        status=status,
        **extra,
    )


def result(*findings: Finding) -> ReviewResult:
    return ReviewResult(
        review=Review(summary="Adds a cache.", files_reviewed=["a.py"], findings=list(findings)),
        verdicts=[],
        metrics=[
            StageMetrics(
                stage="review",
                model="m",
                cost_usd=0.5,
                input_tokens=1,
                output_tokens=1,
                num_turns=1,
                duration_ms=1,
            )
        ],
        total_cost_usd=0.5,
        session_id="sess",
    )


def test_a_tripped_gate_fails_the_check_and_marks_the_findings_that_tripped_it() -> None:
    payload = build_check_run(
        result(finding(1, "critical"), finding(2, "low")), context(), fail_on="high"
    )

    assert payload["name"] == CHECK_NAME
    assert payload["head_sha"] == "a" * 40
    assert payload["status"] == "completed"
    assert payload["conclusion"] == "failure"
    levels = [(a["start_line"], a["annotation_level"]) for a in payload["output"]["annotations"]]
    assert levels == [(1, "failure"), (2, "warning")]


def test_findings_below_the_gate_are_neutral_and_none_at_all_is_success() -> None:
    below = build_check_run(result(finding(1, "low")), context(), fail_on="high")
    clean = build_check_run(result(), context(), fail_on="high")

    assert below["conclusion"] == "neutral"
    assert clean["conclusion"] == "success"
    assert clean["output"]["annotations"] == []


def test_without_a_gate_findings_never_fail_the_check() -> None:
    payload = build_check_run(result(finding(1, "critical")), context(), fail_on=None)

    assert payload["conclusion"] == "neutral"
    assert payload["output"]["annotations"][0]["annotation_level"] == "warning"


def test_rejected_and_known_findings_are_not_annotated_and_known_still_gate() -> None:
    payload = build_check_run(
        result(finding(1, "critical", "rejected"), finding(2, "critical", "known")),
        context(),
        fail_on="high",
    )

    assert payload["output"]["annotations"] == []
    assert payload["conclusion"] == "failure"  # reported before is not fixed


def test_an_annotation_carries_the_finding_and_its_range() -> None:
    payload = build_check_run(result(finding(3, end_line=5)), context(), fail_on=None)

    (annotation,) = payload["output"]["annotations"]
    assert annotation["path"] == "a.py"
    assert (annotation["start_line"], annotation["end_line"]) == (3, 5)
    assert annotation["title"] == "[HIGH] Problem 3"
    assert "What is wrong." in annotation["message"]
    assert "Adds a cache." in payload["output"]["summary"]


def test_one_request_holds_every_annotation_a_review_can_have() -> None:
    # The API takes 50 annotations a request; a review holds at most 25 findings.
    assert MAX_FINDINGS <= MAX_ANNOTATIONS
