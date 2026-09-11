"""Turn a ReviewResult into a pull request review payload.

A finding is an inline comment when its file changed in the pull request
and its line is one the diff shows on the new side; GitHub rejects
comments anywhere else. Every other finding is listed in the review body.
The review never requests changes: merge gating is the CLI exit code.
"""

from typing import Any

from argus.context.diff import commentable_index, parse_diff
from argus.domain.models import Finding, ReviewContext, ReviewResult, rank_findings
from argus.report.markdown import finding_body, render_counts, render_finding, render_footer

REVIEW_MARKER = "<!-- argus:review -->"
REVIEW_EVENT = "COMMENT"


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
