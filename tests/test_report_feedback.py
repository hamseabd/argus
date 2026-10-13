"""What people thought of Argus's posted findings, read back from reactions and replies."""

from argus.report.feedback import collect_feedback, render_feedback, summarize_feedback

LOGIN = "argus[bot]"


def argus_comment(cid: int, pr: int, *, up: int = 0, down: int = 0, **header) -> dict:
    h = {"severity": "HIGH", "title": "SQL built from input", "category": "security"}
    h.update(header)
    status = h.get("status", "confirmed")
    return {
        "id": cid,
        "pull_request_url": f"https://api.github.com/repos/o/r/pulls/{pr}",
        "html_url": f"https://github.com/o/r/pull/{pr}#discussion_r{cid}",
        "path": "app/x.py",
        "line": 12,
        "user": {"login": LOGIN, "type": "Bot"},
        "body": (
            f"**[{h['severity']}] {h['title']}** · {h['category']} · {status} · confidence 0.90"
            "\n\nDetails."
        ),
        "reactions": {"+1": up, "-1": down, "laugh": 0},
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
        argus_comment(2, 7, down=1, category="quality", severity="LOW", status="unverified"),
        reply(3, to=1),
        reply(4, to=1, login=LOGIN),  # Argus replying to itself is not feedback
    ]

    records = collect_feedback(comments, LOGIN)

    assert [
        (r.pr, r.category, r.status, r.thumbs_up, r.thumbs_down, r.replies) for r in records
    ] == [
        (7, "security", "confirmed", 2, 0, 1),
        (7, "quality", "unverified", 0, 1, 0),
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


def test_the_summary_is_human_precision_by_status_and_category() -> None:
    records = collect_feedback(
        [
            argus_comment(1, 7, up=1),
            argus_comment(2, 8, down=1),
            argus_comment(3, 8, up=1),
            argus_comment(4, 9, category="quality", down=2),
            argus_comment(5, 9, category="quality"),  # unrated
        ],
        LOGIN,
    )

    summary = summarize_feedback(records)

    assert summary.findings == 5
    assert summary.rated == 4
    security = summary.by_category["security"]
    assert (security.helpful, security.not_helpful, security.unrated) == (2, 1, 0)
    assert security.precision == 2 / 3
    quality = summary.by_category["quality"]
    assert (quality.helpful, quality.not_helpful, quality.unrated) == (0, 1, 1)
    assert summary.by_status["confirmed"].precision == 2 / 4


def test_a_finding_with_more_downs_than_ups_counts_as_not_helpful() -> None:
    records = collect_feedback([argus_comment(1, 7, up=1, down=2)], LOGIN)

    summary = summarize_feedback(records)

    assert summary.by_category["security"].not_helpful == 1


def test_the_rendered_table_has_a_row_per_category_and_status() -> None:
    records = collect_feedback([argus_comment(1, 7, up=1), argus_comment(2, 7, down=1)], LOGIN)

    text = render_feedback(summarize_feedback(records))

    assert "| security | 1 | 1 | 0 | 50% |" in text
    assert "| confirmed | 1 | 1 | 0 | 50% |" in text
    assert "2 findings posted, 2 rated" in text


def test_a_comment_posted_before_findings_carried_a_status_still_counts() -> None:
    old = argus_comment(1, 7, up=1) | {"body": "**[HIGH] Leak** · correctness · confidence 0.90"}

    (record,) = collect_feedback([old], LOGIN)

    assert (record.title, record.category, record.status) == ("Leak", "correctness", "")
