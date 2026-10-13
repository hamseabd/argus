"""Turn a ReviewResult into a pull request review payload.

A finding is an inline comment when its file changed in the pull request
and its line is one the diff shows on the new side; GitHub rejects
comments anywhere else. Every other finding is listed in the review body.
The review never requests changes: merge gating is the CLI exit code.

The same module reads the format back: known_findings_from() turns the
reviews and inline comments already on a pull request into the findings an
earlier Argus run posted, so a re-review does not repeat them. An Argus review
is one that opens with REVIEW_MARKER and was posted by the one login Argus
itself posts as, which the caller names; the marker alone, which anyone could
type, is not enough, and neither is being some bot: every GitHub App installed
on the repository is one.
"""

import re
from collections.abc import Iterable
from typing import Any

from argus.context.diff import commentable_index, parse_diff
from argus.domain.models import (
    SEVERITY_ORDER,
    Category,
    Finding,
    KnownFinding,
    ReviewContext,
    ReviewResult,
    Severity,
    rank_findings,
)
from argus.report.markdown import finding_body, render_counts, render_finding, render_footer

REVIEW_MARKER = "<!-- argus:review -->"
REVIEW_EVENT = "COMMENT"
_SEVERITIES = "|".join(s.upper() for s in SEVERITY_ORDER)
_INLINE_TITLE = re.compile(
    rf"^\*\*\[(?P<severity>{_SEVERITIES})\] (?P<title>.+?)\*\* · (?P<category>\w+) · "
)
"""The first line finding_body() writes: **[SEVERITY] title** · category · status · confidence.

The title ends at the `** · ` the writer puts after it, so a title that itself
holds `**` (a `**kwargs`, say) is read whole.
"""
_BODY_TITLE = re.compile(rf"^#{{2,3}} \[(?P<severity>{_SEVERITIES})\] (?P<title>.+?)\s*$")
"""The heading render_finding() writes for a finding listed in the review body."""
_BODY_LOCATION = re.compile(r"^`(?P<file>[^`:]+):(?P<line>\d+)(?:-\d+)?` · (?P<category>\w+) · ")
"""The location line under that heading: `path:line` · category · status · confidence."""


def build_review(result: ReviewResult, context: ReviewContext) -> dict[str, Any]:
    if context.pr is None:
        raise ValueError("a GitHub review needs a pull request context")
    index = commentable_index(parse_diff(context.diff_text))
    comments: list[dict[str, Any]] = []
    in_body: list[Finding] = []
    for finding in rank_findings(result.review.findings):
        lines = index.get(finding.file, frozenset())
        if finding.line in lines:
            comments.append(_comment(finding, lines))
        else:
            in_body.append(finding)
    return {
        "commit_id": context.pr.head_sha,
        "event": REVIEW_EVENT,
        "body": _body(result, in_body),
        "comments": comments,
    }


def known_findings_from(
    reviews: Iterable[dict[str, Any]], comments: Iterable[dict[str, Any]], reviewer_login: str
) -> list[KnownFinding]:
    """The findings earlier Argus reviews posted on a pull request, inline and in the body.

    `reviews` and `comments` are the GitHub API's pull request reviews and
    review comments; `reviewer_login` is the login Argus posts as, and with it
    empty nothing is trusted. For each Argus review, in order: its inline
    comments by that login, taking the current line or, for a comment outdated
    by a later push, the line it was posted on; then the findings its body
    lists as off the diff. Replies by anyone else in the same thread are not
    findings. A comment or heading whose first line does not read as one the
    report module writes is skipped, never guessed at.
    """
    if not reviewer_login:
        return []
    argus_reviews = [r for r in reviews if _is_argus_review(r, reviewer_login)]
    by_review: dict[Any, list[dict[str, Any]]] = {r.get("id"): [] for r in argus_reviews}
    for comment in comments:
        review_id = comment.get("pull_request_review_id")
        if review_id in by_review and _posted_by(comment.get("user"), reviewer_login):
            by_review[review_id].append(comment)
    known: list[KnownFinding] = []
    for review in argus_reviews:
        for comment in by_review[review.get("id")]:
            match = _INLINE_TITLE.match(str(comment.get("body") or ""))
            if match is None:
                continue
            line = comment.get("line") or comment.get("original_line")
            known.append(
                KnownFinding(
                    file=str(comment.get("path") or ""),
                    line=int(line) if line else None,
                    title=match["title"],
                    severity=_severity(match["severity"]),
                    category=_category(match["category"]),
                    url=str(comment.get("html_url") or ""),
                )
            )
        known.extend(
            _body_findings(str(review.get("body") or ""), str(review.get("html_url") or ""))
        )
    return known


def _is_argus_review(review: dict[str, Any], reviewer_login: str) -> bool:
    body = str(review.get("body") or "").lstrip()
    return body.startswith(REVIEW_MARKER) and _posted_by(review.get("user"), reviewer_login)


def _posted_by(user: Any, reviewer_login: str) -> bool:
    return (
        isinstance(user, dict)
        and user.get("type") == "Bot"
        and str(user.get("login") or "") == reviewer_login
    )


def _body_findings(body: str, url: str) -> list[KnownFinding]:
    """Findings a review body lists under "Findings not on the diff"."""
    found: list[KnownFinding] = []
    lines = body.splitlines()
    for i, line in enumerate(lines):
        heading = _BODY_TITLE.match(line)
        if heading is None:
            continue
        rest = (text for text in lines[i + 1 :] if text.strip())
        location = _BODY_LOCATION.match(next(rest, ""))
        if location is None:
            continue
        found.append(
            KnownFinding(
                file=location["file"],
                line=int(location["line"]),
                title=heading["title"],
                severity=_severity(heading["severity"]),
                category=_category(location["category"]),
                url=url,
            )
        )
    return found


def _severity(tag: str) -> Severity | None:
    value = tag.lower()
    return value if value in SEVERITY_ORDER else None  # type: ignore[return-value]


def _category(word: str) -> Category | None:
    return word if word in ("correctness", "security", "quality") else None  # type: ignore[return-value]


def _comment(finding: Finding, lines: frozenset[int]) -> dict[str, Any]:
    comment: dict[str, Any] = {"path": finding.file, "side": "RIGHT", "body": finding_body(finding)}
    end = finding.end_line
    if (
        end is not None
        and end != finding.line
        and all(n in lines for n in range(finding.line, end + 1))
    ):
        comment |= {"start_line": finding.line, "start_side": "RIGHT", "line": end}
    else:
        comment["line"] = finding.line
    return comment


def _body(result: ReviewResult, in_body: list[Finding]) -> str:
    parts = [REVIEW_MARKER, result.review.summary.strip(), render_counts(result.review.findings)]
    if in_body:
        parts.append("## Findings not on the diff")
        parts.extend(render_finding(f, heading="###") for f in in_body)
    parts.append("---")
    parts.append(render_footer(result))
    return "\n\n".join(parts) + "\n"
