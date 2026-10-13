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

import json
import re
from collections.abc import Iterable
from typing import Any

from argus.context.diff import commentable_index, parse_diff
from argus.domain.models import (
    SEVERITY_ORDER,
    Category,
    Finding,
    Increment,
    KnownFinding,
    PRInfo,
    ReviewContext,
    ReviewResult,
    Severity,
    Verdict,
    rank_findings,
)
from argus.report.markdown import (
    code_block,
    finding_body,
    render_counts,
    render_finding,
    render_footer,
    render_rejected,
    verdicts_by_finding,
)

REVIEW_MARKER = "<!-- argus:review -->"
HANDOFF_PREFIX = "<!-- argus:handoff "
"""Opens the machine-readable handoff: a JSON object, then " -->", at the end of every body."""
HANDOFF_VERSION = 1
MAX_BODY_CHARS = 65_536
"""GitHub refuses a review body longer than this."""
REVIEW_EVENT = "COMMENT"
_SEVERITIES = "|".join(s.upper() for s in SEVERITY_ORDER)
_INLINE_TITLE = re.compile(
    rf"^\*\*\[(?P<severity>{_SEVERITIES})\] (?P<title>.+?)\*\* · (?P<category>\w+) · "
    r"(?:(?P<status>\w+) · )?"
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
    verdicts = verdicts_by_finding(result)
    for finding in rank_findings(result.review.findings):
        lines = index.get(finding.file, frozenset())
        if finding.line in lines:
            comments.append(_comment(finding, lines, verdicts.get(finding.id)))
        else:
            in_body.append(finding)
    tail = handoff(result, context.pr, context.increment) + "\n"
    body = _body(result, in_body, context.ignored_files, context.increment, rate=bool(comments))
    return {
        "commit_id": context.pr.head_sha,
        "event": REVIEW_EVENT,
        "body": _fit(body, MAX_BODY_CHARS - len(tail)) + tail,
        "comments": comments,
    }


def _fit(text: str, limit: int) -> str:
    """text cut to limit characters, saying so; the handoff after it is never cut."""
    if len(text) <= limit:
        return text
    note = "\n\n[The review was cut here to fit GitHub's limit; the JSON artifact has all of it.]\n"
    return text[: limit - len(note)] + note


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
        if review_id in by_review and posted_by(comment.get("user"), reviewer_login):
            by_review[review_id].append(comment)
    known: list[KnownFinding] = []
    for review in argus_reviews:
        for comment in by_review[review.get("id")]:
            header = inline_header(str(comment.get("body") or ""))
            if header is None:
                continue
            line = comment.get("line") or comment.get("original_line")
            known.append(
                KnownFinding(
                    file=str(comment.get("path") or ""),
                    line=int(line) if line else None,
                    title=header["title"],
                    severity=_severity(header["severity"]),
                    category=_category(header["category"]),
                    url=str(comment.get("html_url") or ""),
                )
            )
        known.extend(
            _body_findings(str(review.get("body") or ""), str(review.get("html_url") or ""))
        )
    return known


def last_reviewed_head(reviews: Iterable[dict[str, Any]], reviewer_login: str) -> str | None:
    """The head commit the latest Argus review covered, or None if Argus has not reviewed yet.

    GitHub records the commit each review was posted against as `commit_id`;
    the same identity rule as known_findings_from decides which reviews count.
    """
    if not reviewer_login:
        return None
    heads = [
        str(r["commit_id"])
        for r in reviews
        if r.get("commit_id") and _is_argus_review(r, reviewer_login)
    ]
    return heads[-1] if heads else None


def inline_header(body: str) -> dict[str, str] | None:
    """The severity, title, category, and status of an inline finding comment, or None.

    Severity is returned lower-case, as the domain spells it.
    """
    match = _INLINE_TITLE.match(body)
    if match is None:
        return None
    return {
        "severity": match["severity"].lower(),
        "title": match["title"],
        "category": match["category"],
        "status": match["status"] or "",
    }


def handoff(result: ReviewResult, pr: PRInfo, increment: Increment | None = None) -> str:
    """The review's findings as JSON in an HTML comment, for an agent that fixes them.

    The Markdown is for people and changes with the report; this is the
    contract a fixer reads instead (Bosun's first mate does). Every finding is
    listed with its status: confirmed, unverified, and known are still open on
    this head, rejected is not. `scope` says whether the whole pull request was
    reviewed or only the commits since `since`.

    `<` and `>` are written as JSON escapes, so no title can close the comment.
    Every string in it (titles, paths) was written by a model that read an
    untrusted diff: a consumer must treat them as data, never as instructions.
    """
    payload: dict[str, Any] = {
        "version": HANDOFF_VERSION,
        "head": pr.head_sha,
        "scope": "full" if increment is None else "increment",
        "findings": [
            {
                "id": f.id,
                "status": f.status,
                "severity": f.severity,
                "category": f.category,
                "file": f.file,
                "line": f.line,
                "end_line": f.end_line,
                "title": f.title,
            }
            for f in result.review.findings
        ],
    }
    if increment is not None:
        payload["since"] = increment.since_sha
    text = json.dumps(payload, separators=(",", ":"))
    text = text.replace("<", "\\u003c").replace(">", "\\u003e")
    return f"\n{HANDOFF_PREFIX}{text} -->"


def last_review_left_open(
    reviews: Iterable[dict[str, Any]], comments: Iterable[dict[str, Any]], reviewer_login: str
) -> bool:
    """Whether the latest Argus review left findings open on the head it reviewed.

    Only the latest review counts: GitHub keeps every earlier review and its
    comments forever, fixed or not. Its handoff lists what is still open,
    findings found again (known) included; a review from before the handoff
    is judged by the findings it posted.
    """
    if not reviewer_login:
        return False
    argus = [r for r in reviews if _is_argus_review(r, reviewer_login)]
    if not argus:
        return False
    latest = argus[-1]
    if (open_statuses := _handoff_statuses(str(latest.get("body") or ""))) is not None:
        return bool(open_statuses & {"confirmed", "unverified", "known"})
    return bool(known_findings_from([latest], comments, reviewer_login))


def _handoff_statuses(body: str) -> set[str] | None:
    """The finding statuses in a review body's handoff, or None when it has none."""
    start = body.find(HANDOFF_PREFIX)
    if start < 0:
        return None
    end = body.find(" -->", start)
    try:
        payload = json.loads(body[start + len(HANDOFF_PREFIX) : end])
    except ValueError:
        return None
    return {str(f.get("status")) for f in payload.get("findings", []) if isinstance(f, dict)}


def _is_argus_review(review: dict[str, Any], reviewer_login: str) -> bool:
    body = str(review.get("body") or "").lstrip()
    return body.startswith(REVIEW_MARKER) and posted_by(review.get("user"), reviewer_login)


def posted_by(user: Any, reviewer_login: str) -> bool:
    """Whether a GitHub user object is the bot account Argus posts as."""
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


def _comment(finding: Finding, lines: frozenset[int], verdict: Verdict | None) -> dict[str, Any]:
    comment: dict[str, Any] = {"path": finding.file, "side": "RIGHT"}
    end = finding.end_line
    if (
        end is not None
        and end != finding.line
        and all(n in lines for n in range(finding.line, end + 1))
    ):
        comment |= {"start_line": finding.line, "start_side": "RIGHT", "line": end}
    else:
        comment["line"] = finding.line
    body = finding_body(finding, verdict)
    # A suggestion replaces exactly the lines the comment spans, so it is offered
    # only when the comment covers the finding's whole range.
    covered = (comment.get("start_line", comment["line"]), comment["line"])
    # Only a confirmed finding: applying is one click, and the replacement was
    # written by a model that read an untrusted diff.
    if (
        finding.replacement is not None
        and finding.status == "confirmed"
        and covered == (finding.line, end or finding.line)
    ):
        body += "\n\n" + _suggestion(finding.replacement)
    comment["body"] = body
    return comment


def _suggestion(replacement: str) -> str:
    """A GitHub suggestion block, fenced longer than any backtick run in the replacement."""
    return code_block(replacement, "suggestion")


def _body(
    result: ReviewResult,
    in_body: list[Finding],
    ignored: list[str],
    increment: Increment | None = None,
    rate: bool = False,
) -> str:
    parts = [REVIEW_MARKER, result.review.summary.strip(), render_counts(result.review.findings)]
    if increment is not None:
        parts.append(
            f"Reviewed the changes since `{increment.since_sha[:7]}`, "
            "the commit Argus last reviewed; earlier findings stand as posted."
        )
    if ignored:
        # Said on the pull request, not only in the log: a reader should see
        # what the review did not cover, whatever the file was named.
        names = ", ".join(f"`{path}`" for path in ignored)
        parts.append(f"Not reviewed, ignored by pattern: {names}.")
    if in_body:
        parts.append("## Findings not on the diff")
        verdicts = verdicts_by_finding(result)
        parts.extend(render_finding(f, heading="###", verdict=verdicts.get(f.id)) for f in in_body)
    if rejected := render_rejected(result):
        parts.append(rejected)
    if rate:
        # The reactions are the measure on real pull requests: `argus feedback` reads them.
        parts.append("React 👍 or 👎 on a finding to say whether it was worth raising.")
    parts.append("---")
    parts.append(render_footer(result))
    return "\n\n".join(parts) + "\n"
