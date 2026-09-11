from pathlib import Path

from argus.domain.models import (
    ChangedFile,
    Finding,
    PRInfo,
    Review,
    ReviewContext,
    ReviewResult,
    StageMetrics,
)
from argus.report.github_review import REVIEW_MARKER, build_review

DIFF = (
    "diff --git a/app/cache.py b/app/cache.py\n--- a/app/cache.py\n+++ b/app/cache.py\n"
    "@@ -1,2 +1,4 @@\n a\n+b\n+c\n d\n"
)


def context() -> ReviewContext:
    return ReviewContext(
        source="pr",
        repo_root=Path("/repo"),
        diff_text=DIFF,
        files=[ChangedFile(path="app/cache.py", status="modified")],
        pr=PRInfo(
            owner="o",
            repo="r",
            number=7,
            title="t",
            body="",
            base_ref="main",
            head_ref="feat",
            head_sha="a" * 40,
            html_url="https://github.com/o/r/pull/7",
        ),
    )


def finding(**overrides) -> Finding:
    base = dict(
        id="correctness-1",
        file="app/cache.py",
        line=2,
        severity="high",
        category="correctness",
        title="Stale entry",
        description="The entry is never evicted.",
        evidence="No TTL check.",
        suggested_fix="Check expiry before returning.",
        confidence=0.8,
        status="confirmed",
    )
    return Finding(**{**base, **overrides})


def result(findings: list[Finding]) -> ReviewResult:
    return ReviewResult(
        review=Review(summary="Adds a cache.", files_reviewed=["app/cache.py"], findings=findings),
        verdicts=[],
        metrics=[
            StageMetrics(
                stage="review",
                model="m",
                cost_usd=0.75,
                input_tokens=10,
                output_tokens=5,
                num_turns=4,
                duration_ms=30000,
                subagents_run=3,
            )
        ],
        total_cost_usd=0.75,
        duration_ms=30000,
        session_id="sess",
    )


def test_findings_on_diff_lines_become_inline_comments() -> None:
    payload = build_review(result([finding()]), context())

    assert payload["commit_id"] == "a" * 40
    assert payload["event"] == "COMMENT"
    (comment,) = payload["comments"]
    assert comment == {
        "path": "app/cache.py",
        "line": 2,
        "side": "RIGHT",
        "body": (
            "**[HIGH] Stale entry** · correctness · confirmed · confidence 0.80\n\n"
            "The entry is never evicted.\n\n"
            "**Evidence:** No TTL check.\n\n"
            "**Suggested fix:**\n\n```\nCheck expiry before returning.\n```"
        ),
    }


def test_a_range_within_the_diff_becomes_a_multi_line_comment() -> None:
    payload = build_review(result([finding(line=2, end_line=3)]), context())

    (comment,) = payload["comments"]
    assert comment["start_line"] == 2 and comment["start_side"] == "RIGHT"
    assert comment["line"] == 3 and comment["side"] == "RIGHT"


def test_a_range_whose_end_is_outside_the_diff_collapses_to_its_first_line() -> None:
    payload = build_review(result([finding(line=2, end_line=9)]), context())

    (comment,) = payload["comments"]
    assert "start_line" not in comment
    assert comment["line"] == 2


def test_findings_off_the_diff_are_listed_in_the_body_instead() -> None:
    off_line = finding(id="correctness-2", line=9, title="Off the diff")
    off_file = finding(id="correctness-3", file="app/other.py", line=1, title="Other file")

    payload = build_review(result([finding(), off_line, off_file]), context())

    assert [c["line"] for c in payload["comments"]] == [2]
    body = payload["body"]
    assert "Off the diff" in body and "`app/cache.py:9`" in body
    assert "Other file" in body and "`app/other.py:1`" in body
    assert "Stale entry" not in body.split("## Findings not on the diff")[1]


def test_body_has_marker_summary_counts_and_footer() -> None:
    payload = build_review(result([finding(), finding(id="x", status="rejected")]), context())

    body = payload["body"]
    assert body.startswith(REVIEW_MARKER + "\n")
    assert "Adds a cache." in body
    assert "1 finding: 1 confirmed, 1 rejected and not shown." in body
    assert body.rstrip().endswith("session sess")
    assert "Cost $0.75" in body


def test_rejected_findings_are_never_posted() -> None:
    payload = build_review(result([finding(status="rejected")]), context())

    assert payload["comments"] == []
    assert "Stale entry" not in payload["body"]


def test_unverified_findings_are_labelled() -> None:
    payload = build_review(result([finding(status="unverified")]), context())

    assert "· unverified ·" in payload["comments"][0]["body"]


def test_inline_comments_follow_rank_order() -> None:
    low = finding(id="a", line=3, severity="low", status="confirmed")
    crit = finding(id="b", line=2, severity="critical", status="unverified")
    high = finding(id="c", line=4, severity="high", status="confirmed")

    payload = build_review(result([low, crit, high]), context())

    assert [c["line"] for c in payload["comments"]] == [4, 3, 2]
