"""What the people reading Argus's reviews thought of its findings.

The eval corpus measures Argus on seeded bugs with known answers; this
measures it on real pull requests, from the only judges that matter there.
Every inline finding Argus posts invites a 👍 or 👎 (the review footer says
so). `argus feedback` reads those reactions, and the replies under each
finding, back from GitHub and turns them into a precision per category and
per status: of the findings someone rated, how many were worth posting.

A finding with more 👍 than 👎 is helpful, more 👎 than 👍 is not, and a tie
(including no reactions at all) is unrated. Only comments posted by the one
login Argus posts as are read, the same identity rule as known findings.
"""

import re
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict

from argus.report.github_review import inline_header, posted_by

_PR_NUMBER = re.compile(r"/pulls/(\d+)$")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FindingFeedback(_Model):
    """One posted finding and what people did with it."""

    pr: int | None
    url: str
    path: str
    line: int | None
    severity: str
    category: str
    status: str
    title: str
    thumbs_up: int
    thumbs_down: int
    replies: int

    @property
    def verdict(self) -> str:
        if self.thumbs_up > self.thumbs_down:
            return "helpful"
        if self.thumbs_down > self.thumbs_up:
            return "not_helpful"
        return "unrated"


class Tally(_Model):
    helpful: int = 0
    not_helpful: int = 0
    unrated: int = 0

    @property
    def precision(self) -> float | None:
        rated = self.helpful + self.not_helpful
        return self.helpful / rated if rated else None


class FeedbackSummary(_Model):
    findings: int
    rated: int
    replied: int
    by_category: dict[str, Tally]
    by_status: dict[str, Tally]


def collect_feedback(
    comments: Iterable[dict[str, Any]], reviewer_login: str
) -> list[FindingFeedback]:
    """One record per inline finding Argus posted, with its reactions and human replies."""
    if not reviewer_login:
        return []
    comments = list(comments)
    replies: dict[Any, int] = {}
    for comment in comments:
        parent = comment.get("in_reply_to_id")
        if parent is not None and not posted_by(comment.get("user"), reviewer_login):
            replies[parent] = replies.get(parent, 0) + 1
    records = []
    for comment in comments:
        if comment.get("in_reply_to_id") is not None:
            continue
        if not posted_by(comment.get("user"), reviewer_login):
            continue
        header = inline_header(str(comment.get("body") or ""))
        if header is None:
            continue
        reactions = comment.get("reactions") or {}
        match = _PR_NUMBER.search(str(comment.get("pull_request_url") or ""))
        line = comment.get("line") or comment.get("original_line")
        records.append(
            FindingFeedback(
                pr=int(match.group(1)) if match else None,
                url=str(comment.get("html_url") or ""),
                path=str(comment.get("path") or ""),
                line=int(line) if line else None,
                thumbs_up=int(reactions.get("+1") or 0),
                thumbs_down=int(reactions.get("-1") or 0),
                replies=replies.get(comment.get("id"), 0),
                **header,
            )
        )
    return records


def summarize_feedback(records: Iterable[FindingFeedback]) -> FeedbackSummary:
    records = list(records)
    return FeedbackSummary(
        findings=len(records),
        rated=sum(r.verdict != "unrated" for r in records),
        replied=sum(r.replies > 0 for r in records),
        by_category=_tally(records, "category"),
        by_status=_tally(records, "status"),
    )


def render_feedback(summary: FeedbackSummary) -> str:
    lines = [
        f"{summary.findings} findings posted, {summary.rated} rated, {summary.replied} replied to.",
        "",
    ]
    for title, groups in (("Category", summary.by_category), ("Status", summary.by_status)):
        lines += [
            f"| {title} | Helpful | Not helpful | Unrated | Precision |",
            "|---|---|---|---|---|",
        ]
        for name, tally in groups.items():
            precision = "n/a" if tally.precision is None else f"{tally.precision:.0%}"
            lines.append(
                f"| {name} | {tally.helpful} | {tally.not_helpful} | {tally.unrated} "
                f"| {precision} |"
            )
        lines.append("")
    return "\n".join(lines)


def _tally(records: list[FindingFeedback], key: str) -> dict[str, Tally]:
    counts: dict[str, dict[str, int]] = {}
    for record in records:
        group = counts.setdefault(getattr(record, key) or "unknown", {})
        group[record.verdict] = group.get(record.verdict, 0) + 1
    return {name: Tally(**group) for name, group in sorted(counts.items())}
