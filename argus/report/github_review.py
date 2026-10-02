"""Turn a ReviewResult into a pull request review payload.

A finding is an inline comment when its file changed in the pull request
and its line is one the diff shows on the new side; GitHub rejects
comments anywhere else. Every other finding is listed in the review body.
The review never requests changes: merge gating is the CLI exit code.

The same module reads the format back: known_findings_from() turns the
reviews and inline comments already on a pull request into the findings an
earlier Argus run posted, so a re-review does not repeat them. An Argus review
is one that opens with REVIEW_MARKER and was posted by a bot account, the
app's; the marker alone, which anyone could type, is not enough.
"""

import re
from collections.abc import Iterable
from typing import Any

from argus.context.diff import commentable_index, parse_diff
from argus.domain.models import Finding, KnownFinding, ReviewContext, ReviewResult, rank_findings
from argus.report.markdown import finding_body, render_counts, render_finding, render_footer

REVIEW_MARKER = "<!-- argus:review -->"
REVIEW_EVENT = "COMMENT"
_INLINE_TITLE = re.compile(r"^\*\*\[(?:CRITICAL|HIGH|MEDIUM|LOW)\] (?P<title>.+?)\*\*")
"""The first line finding_body() writes: **[SEVERITY] title** · meta."""
_BODY_TITLE = re.compile(r"^#{2,3} \[(?:CRITICAL|HIGH|MEDIUM|LOW)\] (?P<title>.+?)\s*$")
"""The heading render_finding() writes for a finding listed in the review body."""
_BODY_LOCATION = re.compile(r"^`(?P<file>[^`:]+):(?P<line>\d+)(?:-\d+)?`")
"""The location line under that heading: `path:line` or `path:start-end`."""


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
    reviews: Iterable[dict[str, Any]], comments: Iterable[dict[str, Any]]
) -> list[KnownFinding]:
    """The findings earlier Argus reviews posted on a pull request, inline and in the body.

    `reviews` and `comments` are the GitHub API's pull request reviews and
    review comments. For each Argus review, in order: its inline comments,
    taking the current line or, for a comment outdated by a later push, the
    line it was posted on; then the findings its body lists as off the diff.
    Replies by people in the same thread are not findings.
    """
    argus_reviews = [r for r in reviews if _is_argus_review(r)]
    by_review: dict[Any, list[dict[str, Any]]] = {r.get("id"): [] for r in argus_reviews}
    for comment in comments:
        review_id = comment.get("pull_request_review_id")
        if review_id in by_review and _is_bot(comment.get("user")):
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
                    url=str(comment.get("html_url") or ""),
                )
            )
        known.extend(
            _body_findings(str(review.get("body") or ""), str(review.get("html_url") or ""))
        )
    return known


def _is_argus_review(review: dict[str, Any]) -> bool:
    body = str(review.get("body") or "").lstrip()
    return body.startswith(REVIEW_MARKER) and _is_bot(review.get("user"))


def _is_bot(user: Any) -> bool:
    return isinstance(user, dict) and user.get("type") == "Bot"


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
                url=url,
            )
        )
    return found


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
