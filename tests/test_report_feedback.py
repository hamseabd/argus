"""What happened to Argus's posted findings: reactions, resolution, and later diffs."""

import json

from argus.report.feedback import collect_feedback, render_feedback, summarize_feedback
from argus.report.github_review import HANDOFF_PREFIX, REVIEW_MARKER

LOGIN = "argus[bot]"


def argus_comment(
    cid: int, pr: int, *, up: int = 0, down: int = 0, review: int = 50, finding: str = "", **header
) -> dict:
    h = {"severity": "HIGH", "title": "SQL built from input", "category": "security"}
    h.update(header)
    status = h.get("status", "confirmed")
    marker = f"\n\n<!-- argus:finding {finding} -->" if finding else ""
    return {
        "id": cid,
        "pull_request_review_id": review,
        "pull_request_url": f"https://api.github.com/repos/o/r/pulls/{pr}",
        "html_url": f"https://github.com/o/r/pull/{pr}#discussion_r{cid}",
        "path": "app/x.py",
        "line": 12,
        "user": {"login": LOGIN, "type": "Bot"},
        "body": (
            f"**[{h['severity']}] {h['title']}** · {h['category']} · {status} · confidence 0.90"
            f"\n\nDetails.{marker}"
        ),
        "reactions": {"+1": up, "-1": down, "laugh": 0},
    }


def argus_review(rid: int, records: list[dict], login: str = LOGIN) -> dict:
    handoff = json.dumps({"version": 2, "findings": records})
    return {
        "id": rid,
        "user": {"login": login, "type": "Bot"},
        "body": f"{REVIEW_MARKER}\n\nSummary.\n{HANDOFF_PREFIX}{handoff} -->",
    }


def reply(cid: int, to: int, login: str = "hamse") -> dict:
    return {
        "id": cid,
        "in_reply_to_id": to,
        "user": {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"},
        "body": "Fixed, thanks.",
        "reactions": {"+1": 0, "-1": 0},
    }


def test_each_argus_finding_comment_becomes_one_record_with_its_reactions() -> None:
    comments = [
        argus_comment(1, 7, up=2),
        argus_comment(
            2, 7, down=1, category="maintainability", severity="LOW", status="unverified"
        ),
        reply(3, to=1),
        reply(4, to=1, login=LOGIN),  # Argus replying to itself is not feedback
    ]

    records = collect_feedback(comments, LOGIN)

    assert [
        (r.pr, r.category, r.status, r.thumbs_up, r.thumbs_down, r.replies) for r in records
    ] == [
        (7, "security", "confirmed", 2, 0, 1),
        (7, "maintainability", "unverified", 0, 1, 0),
    ]
    assert records[0].url.endswith("#discussion_r1")
    assert records[0].severity == "high"


def test_comments_by_anyone_else_or_not_in_the_finding_format_are_skipped() -> None:
    impostor = argus_comment(1, 7) | {"user": {"login": "mallory", "type": "User"}}
    other_bot = argus_comment(2, 7) | {"user": {"login": "lint[bot]", "type": "Bot"}}
    chatter = argus_comment(3, 7) | {"body": "Thanks for the fix!"}

    assert collect_feedback([impostor, other_bot, chatter], LOGIN) == []


def test_without_an_identity_nothing_is_trusted() -> None:
    assert collect_feedback([argus_comment(1, 7, up=1)], "") == []


def test_the_specialist_and_confidence_come_from_the_handoff_by_comment_id() -> None:
    review = argus_review(
        50, [{"id": "security-1", "specialist": "design", "confidence": 0.7, "comment": 1}]
    )

    (record,) = collect_feedback([argus_comment(1, 7)], LOGIN, reviews=[review])

    assert (record.specialist, record.confidence) == ("design", 0.7)


def test_without_a_comment_id_the_finding_id_the_comment_ends_with_joins_it() -> None:
    review = argus_review(
        50, [{"id": "security-1", "specialist": "tests", "confidence": 0.65, "comment": None}]
    )
    comment = argus_comment(1, 7, finding="security-1")

    (record,) = collect_feedback([comment], LOGIN, reviews=[review])

    assert (record.specialist, record.confidence) == ("tests", 0.65)


def test_a_review_from_before_the_handoff_falls_back_to_the_category() -> None:
    (record,) = collect_feedback([argus_comment(1, 7, category="design")], LOGIN, reviews=[])

    assert (record.specialist, record.confidence) == ("design", None)


def test_a_failed_run_s_review_on_the_same_pull_request_is_read_past() -> None:
    failed = argus_review(51, [])
    failed["body"] = failed["body"].replace('"findings": []', '"findings": null')

    (record,) = collect_feedback([argus_comment(1, 7)], LOGIN, reviews=[failed])

    assert record.specialist == "security"


def test_a_handoff_in_a_review_by_anyone_else_is_ignored() -> None:
    forged = argus_review(
        50, [{"id": "x", "specialist": "design", "comment": 1}], login="mallory[bot]"
    )

    (record,) = collect_feedback([argus_comment(1, 7)], LOGIN, reviews=[forged])

    assert record.specialist == "security"


def test_a_resolved_or_outdated_thread_counts_as_acted_on() -> None:
    threads = [
        {"comment_id": 1, "resolved": True, "outdated": False},
        {"comment_id": 2, "resolved": False, "outdated": True},
    ]
    comments = [argus_comment(1, 7), argus_comment(2, 7), argus_comment(3, 7)]

    records = collect_feedback(comments, LOGIN, threads=threads)

    assert [(r.resolved, r.changed, r.acted_on) for r in records] == [
        (True, False, True),
        (False, True, True),
        (False, False, False),
    ]


def test_a_thumbs_down_is_a_false_positive_even_when_the_thread_was_resolved() -> None:
    threads = [{"comment_id": 1, "resolved": True, "outdated": True}]

    (record,) = collect_feedback([argus_comment(1, 7, up=1, down=2)], LOGIN, threads=threads)

    assert record.false_positive
    assert not record.acted_on


def test_a_thumbs_up_alone_is_acted_on() -> None:
    (record,) = collect_feedback([argus_comment(1, 7, up=1)], LOGIN)

    assert record.acted_on and not record.false_positive


def test_the_summary_gives_rates_per_specialist_and_per_severity() -> None:
    review = argus_review(50, [{"id": "a", "specialist": "design", "comment": 4}])
    records = collect_feedback(
        [
            argus_comment(1, 7, up=1),
            argus_comment(2, 8, down=1),
            argus_comment(3, 8),
            argus_comment(4, 9, severity="LOW", up=1),
        ],
        LOGIN,
        reviews=[review],
    )

    summary = summarize_feedback(records)

    assert (summary.findings, summary.acted_on, summary.false_positives) == (4, 2, 1)
    security = summary.by_specialist["security"]
    assert (security.posted, security.acted_on, security.false_positives) == (3, 1, 1)
    assert security.acted_on_rate == 1 / 3
    assert summary.by_specialist["design"].acted_on_rate == 1.0
    assert list(summary.by_severity) == ["high", "low"]  # most severe first
    assert summary.by_severity["high"].false_positive_rate == 1 / 3


def test_the_rendered_tables_hold_counts_and_rates_only() -> None:
    records = collect_feedback([argus_comment(1, 7, up=1), argus_comment(2, 7, down=1)], LOGIN)

    text = render_feedback(summarize_feedback(records))

    assert "2 findings posted inline, 1 acted on, 1 marked false positive." in text
    assert "| security | 2 | 1 | 1 | 50% | 50% |" in text
    assert "| high | 2 | 1 | 1 | 50% | 50% |" in text
    assert "SQL built from input" not in text and "app/x.py" not in text


def test_a_comment_posted_before_findings_carried_a_status_still_counts() -> None:
    old = argus_comment(1, 7, up=1) | {"body": "**[HIGH] Leak** · correctness · confidence 0.90"}

    (record,) = collect_feedback([old], LOGIN)

    assert (record.title, record.category, record.status) == ("Leak", "correctness", "")


def test_a_comment_that_says_which_agent_found_it_is_attributed_without_a_handoff() -> None:
    comment = argus_comment(1, 7)
    comment["body"] = comment["body"].replace("confidence 0.90", "confidence 0.90 · found by lead")

    (record,) = collect_feedback([comment], LOGIN)

    assert record.specialist == "lead"


def test_the_finding_id_join_is_scoped_to_the_review_the_comment_belongs_to() -> None:
    first = argus_review(50, [{"id": "security-1", "specialist": "design", "comment": None}])
    second = argus_review(51, [{"id": "security-1", "specialist": "tests", "comment": None}])
    comment = argus_comment(1, 7, review=51, finding="security-1")

    (record,) = collect_feedback([comment], LOGIN, reviews=[first, second])

    assert record.specialist == "tests"
