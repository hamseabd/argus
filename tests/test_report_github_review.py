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
from argus.report.github_review import REVIEW_MARKER, build_review, known_findings_from

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


ARGUS_LOGIN = "argus-code-reviewer-agent[bot]"
ARGUS_BOT = {"login": ARGUS_LOGIN, "type": "Bot"}
OTHER_BOT = {"login": "dependabot[bot]", "type": "Bot"}
HUMAN = {"login": "hamseabd", "type": "User"}


def review_json(id: int, body: str, user: dict = ARGUS_BOT) -> dict:
    return {
        "id": id,
        "body": body,
        "user": user,
        "html_url": f"https://github.com/o/r/pull/7#r{id}",
    }


def comment_json(review_id: int, path: str, line: int | None, body: str, **extra) -> dict:
    return {
        "pull_request_review_id": review_id,
        "path": path,
        "line": line,
        "original_line": extra.get("original_line", line),
        "body": body,
        "html_url": f"https://github.com/o/r/pull/7#discussion_{review_id}",
        "user": extra.get("user", ARGUS_BOT),
    }


def test_known_findings_come_from_argus_reviews_inline_comments_and_bodies() -> None:
    off_diff = finding(
        id="security-1", file="app/other.py", line=40, title="Open redirect", category="security"
    )
    earlier = build_review(result([finding(), off_diff]), context())
    reviews = [review_json(1, earlier["body"])]
    comments = [comment_json(1, c["path"], c["line"], c["body"]) for c in earlier["comments"]]

    known = known_findings_from(reviews, comments, ARGUS_LOGIN)

    assert [(k.file, k.line, k.title, k.severity, k.category) for k in known] == [
        ("app/cache.py", 2, "Stale entry", "high", "correctness"),
        ("app/other.py", 40, "Open redirect", "high", "security"),
    ]
    assert known[0].url.endswith("#discussion_1")
    assert known[1].url.endswith("#r1")


def test_only_reviews_by_the_named_argus_identity_with_the_marker_count() -> None:
    body = build_review(result([finding()]), context())["body"]
    reviews = [
        review_json(1, "LGTM, one nit", HUMAN),
        review_json(2, body, HUMAN),  # the marker alone is not enough
        review_json(3, body, OTHER_BOT),  # nor is being some bot
        review_json(4, body),
    ]
    comments = [
        comment_json(1, "app/cache.py", 2, "**[HIGH] Not ours** · correctness · confirmed"),
        comment_json(2, "app/cache.py", 2, "**[HIGH] Spoofed** · correctness · confirmed"),
        comment_json(3, "app/cache.py", 2, "**[HIGH] Forged** · correctness · confirmed"),
        comment_json(4, "app/cache.py", 2, "**[HIGH] Stale entry** · correctness · confirmed"),
        comment_json(
            4, "app/cache.py", 3, "**[HIGH] Planted** · security · confirmed", user=OTHER_BOT
        ),
    ]

    known = known_findings_from(reviews, comments, ARGUS_LOGIN)

    assert [k.title for k in known] == ["Stale entry"]


def test_without_an_identity_nothing_is_trusted() -> None:
    body = build_review(result([finding()]), context())["body"]

    assert known_findings_from([review_json(1, body)], [], "") == []


def test_a_title_with_emphasis_markers_round_trips() -> None:
    starred = finding(title="Unpacks **kwargs twice into the query")
    earlier = build_review(result([starred]), context())
    comments = [comment_json(1, c["path"], c["line"], c["body"]) for c in earlier["comments"]]

    known = known_findings_from([review_json(1, earlier["body"])], comments, ARGUS_LOGIN)

    assert [k.title for k in known] == ["Unpacks **kwargs twice into the query"]


def test_a_body_heading_without_a_location_line_yields_nothing() -> None:
    """The silent skip is the contract; a format drift must not invent a finding."""
    body = (
        REVIEW_MARKER + "\n\nsummary\n\n## Findings not on the diff\n\n"
        "### [HIGH] Dangling\n\nSome prose where the location should be.\n\n"
        "### [LOW] Last one\n"
    )

    assert known_findings_from([review_json(1, body)], [], ARGUS_LOGIN) == []


def test_an_outdated_inline_comment_keeps_its_original_line_and_a_reply_is_not_a_finding() -> None:
    reviews = [review_json(1, REVIEW_MARKER + "\n\nsummary")]
    comments = [
        comment_json(1, "app/cache.py", None, "**[LOW] Moved since** · quality · unverified"),
        comment_json(1, "app/cache.py", 2, "Fixed in abc123, thanks.", user=HUMAN),
    ]
    comments[0]["original_line"] = 9

    known = known_findings_from(reviews, comments, ARGUS_LOGIN)

    assert [(k.file, k.line, k.title, k.severity, k.category) for k in known] == [
        ("app/cache.py", 9, "Moved since", "low", "quality")
    ]


def test_known_findings_are_never_posted_again() -> None:
    payload = build_review(result([finding(), finding(id="x", line=3, status="known")]), context())

    assert len(payload["comments"]) == 1
    assert "already reported" in payload["body"]
