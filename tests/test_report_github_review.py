import json
from pathlib import Path

from argus.domain.models import (
    ChangedFile,
    Finding,
    Increment,
    PRInfo,
    Review,
    ReviewContext,
    ReviewResult,
    StageMetrics,
    Verdict,
)
from argus.report.github_review import (
    HANDOFF_PREFIX,
    REVIEW_MARKER,
    build_review,
    known_findings_from,
    last_reviewed_head,
)

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
    body = payload["body"].split(HANDOFF_PREFIX)[0]
    assert "Off the diff" in body and "`app/cache.py:9`" in body
    assert "Other file" in body and "`app/other.py:1`" in body
    assert "Stale entry" not in body.split("## Findings not on the diff")[1]


def test_body_has_marker_summary_counts_and_footer() -> None:
    payload = build_review(result([finding(), finding(id="x", status="rejected")]), context())

    body = payload["body"].split(HANDOFF_PREFIX)[0]  # the handoff follows the footer
    assert body.startswith(REVIEW_MARKER + "\n")
    assert "Adds a cache." in body
    assert "1 finding: 1 confirmed, 1 rejected by the verifier." in body
    assert body.rstrip().endswith("session sess")
    assert "Cost $0.75" in body


def test_rejected_findings_are_never_posted_as_findings() -> None:
    payload = build_review(result([finding(status="rejected")]), context())

    assert payload["comments"] == []
    # Only in the folded list of rejections, never as a finding of its own.
    before, folded = payload["body"].split("<details>")
    assert "Stale entry" not in before
    assert "Stale entry" in folded.split("</details>")[0]


def test_unverified_findings_are_labelled() -> None:
    payload = build_review(result([finding(status="unverified")]), context())

    assert "· unverified ·" in payload["comments"][0]["body"]


def test_inline_comments_follow_rank_order() -> None:
    low = finding(id="a", line=3, severity="low", status="confirmed")
    crit = finding(id="b", line=2, severity="critical", status="unverified")
    high = finding(id="c", line=4, severity="high", status="confirmed")

    payload = build_review(result([low, crit, high]), context())

    assert [c["line"] for c in payload["comments"]] == [4, 3, 2]


def test_the_body_names_the_files_the_review_left_out() -> None:
    ctx = context().model_copy(update={"ignored_files": ["uv.lock", "dist/app.min.js"]})

    body = build_review(result([finding()]), ctx)["body"]
    plain = build_review(result([finding()]), context())["body"]

    assert "Not reviewed, ignored by pattern: `uv.lock`, `dist/app.min.js`." in body
    assert "Not reviewed" not in plain


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


def test_rejected_findings_are_listed_collapsed_and_are_never_read_back_as_known() -> None:
    rejected = finding(id="x", line=9, title="Not a real leak", status="rejected")
    verdict = Verdict(
        finding_id="x",
        verdict="rejected",
        reasoning="The handle is closed by the context manager on line 4.",
        confidence=0.9,
    )
    off_diff = finding(id="correctness-2", line=9, title="Off the diff")
    review = result([finding(), off_diff, rejected]).model_copy(update={"verdicts": [verdict]})

    payload = build_review(review, context())

    body = payload["body"]
    details = body.split("<details>")[1].split("</details>")[0]
    assert "Not a real leak" in details
    assert "closed by the context manager" in details
    assert all("Not a real leak" not in c["body"] for c in payload["comments"])
    known = known_findings_from(
        [{"id": 1, "user": {"login": "argus[bot]", "type": "Bot"}, "body": body, "html_url": "u"}],
        [],
        "argus[bot]",
    )
    assert [k.title for k in known] == ["Off the diff"]  # the parser works; it skips rejections


def argus_review(review_id: int, commit: str, login: str = "argus[bot]") -> dict:
    return {
        "id": review_id,
        "commit_id": commit,
        "body": REVIEW_MARKER + "\n\nEarlier.",
        "user": {"login": login, "type": "Bot"},
        "html_url": f"https://x/{review_id}",
    }


def test_the_last_reviewed_head_is_the_commit_of_the_latest_argus_review() -> None:
    reviews = [
        argus_review(1, "a" * 40),
        argus_review(2, "b" * 40, login="someone-else[bot]"),
        argus_review(3, "c" * 40),
        {
            "id": 4,
            "commit_id": "d" * 40,
            "body": "LGTM",
            "user": {"login": "hamse", "type": "User"},
        },
    ]

    assert last_reviewed_head(reviews, "argus[bot]") == "c" * 40


def test_with_no_earlier_argus_review_or_no_identity_there_is_no_last_head() -> None:
    assert last_reviewed_head([argus_review(1, "a" * 40)], "") is None
    assert last_reviewed_head([], "argus[bot]") is None


def test_an_incremental_review_says_what_it_covered() -> None:
    increment = Increment(since_sha="b" * 40, diff_text="", files=[])
    ctx = context().model_copy(update={"increment": increment})

    body = build_review(result([finding()]), ctx)["body"]

    assert f"Reviewed the changes since `{'b' * 7}`" in body


def test_the_review_asks_readers_to_rate_the_inline_findings() -> None:
    body = build_review(result([finding()]), context())["body"]

    assert "React 👍 or 👎 on a finding" in body


def test_a_review_with_no_inline_finding_asks_for_nothing() -> None:
    body = build_review(result([]), context())["body"]

    assert "React" not in body


def test_a_replacement_becomes_a_one_click_suggestion_on_its_line() -> None:
    fixed = finding(replacement="    return cache.get(key, default)")

    (comment,) = build_review(result([fixed]), context())["comments"]

    assert "```suggestion\n    return cache.get(key, default)\n```" in comment["body"]


def test_a_multi_line_replacement_suggests_over_the_whole_range() -> None:
    fixed = finding(line=2, end_line=3, replacement="a = 1\nb = 2")

    (comment,) = build_review(result([fixed]), context())["comments"]

    assert (comment["start_line"], comment["line"]) == (2, 3)
    assert "```suggestion\na = 1\nb = 2\n```" in comment["body"]


def test_no_suggestion_when_the_comment_cannot_cover_the_replaced_range() -> None:
    # The range runs off the diff, so the comment collapses to its first line;
    # a suggestion there would replace one line with what was meant for several.
    fixed = finding(line=2, end_line=9, replacement="a = 1")

    (comment,) = build_review(result([fixed]), context())["comments"]

    assert "suggestion" not in comment["body"]


def test_a_replacement_holding_backticks_gets_a_longer_fence() -> None:
    fixed = finding(line=2, end_line=4, replacement='doc = """\n```py\nx = 1\n```"""')

    (comment,) = build_review(result([fixed]), context())["comments"]

    assert "````suggestion\n" in comment["body"]


def test_an_empty_replacement_suggests_deleting_the_lines() -> None:
    (comment,) = build_review(result([finding(replacement="")]), context())["comments"]

    assert "```suggestion\n```" in comment["body"]


def handoff_of(body: str) -> dict:
    payload = body.split(HANDOFF_PREFIX, 1)[1].split(" -->", 1)[0]
    return json.loads(payload)


def test_every_review_carries_a_machine_readable_handoff_of_its_findings() -> None:
    # A fixer (Bosun's first mate) reads this instead of scraping the Markdown.
    known = finding(id="correctness-2", line=9, title="Still open", status="known")
    rejected = finding(id="correctness-3", line=3, title="Not real", status="rejected")

    body = build_review(result([finding(), known, rejected]), context())["body"]

    handoff = handoff_of(body)
    assert handoff["version"] == 1
    assert handoff["head"] == "a" * 40
    assert handoff["scope"] == "full"
    rows = [
        (f["id"], f["status"], f["severity"], f["file"], f["line"]) for f in handoff["findings"]
    ]
    assert rows == [
        ("correctness-1", "confirmed", "high", "app/cache.py", 2),
        ("correctness-2", "known", "high", "app/cache.py", 9),
        ("correctness-3", "rejected", "high", "app/cache.py", 3),
    ]
    assert handoff["findings"][0]["title"] == "Stale entry"


def test_the_handoff_cannot_be_closed_early_by_a_title() -> None:
    sneaky = finding(title="ends the comment --> <script>")

    body = build_review(result([sneaky]), context())["body"]

    assert body.count("-->") == body.count("<!--")  # only the markers close
    assert handoff_of(body)["findings"][0]["title"] == "ends the comment --> <script>"


def test_an_incremental_review_says_so_in_the_handoff() -> None:
    increment = Increment(since_sha="b" * 40, diff_text="", files=[])
    ctx = context().model_copy(update={"increment": increment})

    handoff = handoff_of(build_review(result([finding()]), ctx)["body"])

    assert (handoff["scope"], handoff["since"]) == ("increment", "b" * 40)


def test_an_inline_finding_posted_without_a_status_is_still_known() -> None:
    review = {
        "id": 1,
        "user": {"login": "argus[bot]", "type": "Bot"},
        "body": REVIEW_MARKER + "\n\nEarlier.",
        "html_url": "u",
    }
    comment = {
        "pull_request_review_id": 1,
        "path": "a.py",
        "line": 4,
        "body": "**[HIGH] Leak** · correctness · confidence 0.90",
        "user": {"login": "argus[bot]", "type": "Bot"},
        "html_url": "c",
    }

    (known,) = known_findings_from([review], [comment], "argus[bot]")

    assert (known.title, known.severity, known.category, known.line) == (
        "Leak",
        "high",
        "correctness",
        4,
    )


def test_only_a_confirmed_finding_gets_a_one_click_suggestion() -> None:
    # Applying is one click; the replacement was written by a model reading
    # an untrusted diff, so it is offered only once the verifier confirmed it.
    unverified = finding(status="unverified", replacement="x = 2")

    (comment,) = build_review(result([unverified]), context())["comments"]

    assert "suggestion" not in comment["body"]


def test_a_replacement_ending_in_a_blank_line_keeps_it() -> None:
    fixed = finding(line=2, end_line=3, replacement="a = 1\n\n")

    (comment,) = build_review(result([fixed]), context())["comments"]

    assert "```suggestion\na = 1\n\n```" in comment["body"]


def test_a_finding_listed_in_the_body_shows_its_replacement() -> None:
    off_diff = finding(line=9, replacement="return None")

    body = build_review(result([off_diff]), context())["body"]

    assert "**Replacement for line 9:**" in body
    assert "return None" in body
