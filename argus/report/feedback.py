"""What happened to Argus's posted findings, read back from the pull requests.

The eval corpus measures Argus on seeded bugs with known answers; this
measures it on real pull requests, from the only judges that matter there.
Three signals, none needing a new UI:

- reactions: 👍 says the finding was right, 👎 says it was a false positive;
- resolution: the review thread was resolved;
- a later diff: a commit after the review changed the flagged lines, which
  GitHub marks by calling the thread outdated.

A finding with more 👎 than 👍 is a false positive. Any other finding with
more 👍 than 👎, a resolved thread, or changed lines was acted on. Rates are
over every finding posted inline, so a finding nobody touched lowers the
acted-on rate: silence is data too.

Each comment is joined to its record in the review's handoff, by the comment
id written there after posting or by the finding id the comment ends with,
for the specialist that reported it and its confidence; a review from before
the handoff carried them falls back to the agent the comment says found it,
then to its category. Only comments
and reviews posted by the one login Argus posts as are read, the same
identity rule as known findings.
"""

import re
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict

from argus.domain.models import SEVERITY_ORDER
from argus.report.github_review import (
    finding_id_of,
    found_by,
    handoff_payload,
    inline_header,
    is_argus_review,
    posted_by,
)

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
    specialist: str
    confidence: float | None
    thumbs_up: int
    thumbs_down: int
    replies: int
    resolved: bool
    changed: bool
    """A later commit changed the flagged lines: GitHub marked the thread outdated."""

    @property
    def false_positive(self) -> bool:
        return is_false_positive(self.thumbs_up, self.thumbs_down)

    @property
    def acted_on(self) -> bool:
        if self.false_positive:
            return False
        return self.thumbs_up > self.thumbs_down or self.resolved or self.changed


class Tally(_Model):
    posted: int = 0
    acted_on: int = 0
    false_positives: int = 0

    @property
    def acted_on_rate(self) -> float | None:
        return self.acted_on / self.posted if self.posted else None

    @property
    def false_positive_rate(self) -> float | None:
        return self.false_positives / self.posted if self.posted else None


class FeedbackSummary(_Model):
    findings: int
    acted_on: int
    false_positives: int
    by_specialist: dict[str, Tally]
    by_severity: dict[str, Tally]


def collect_feedback(
    comments: Iterable[dict[str, Any]],
    reviewer_login: str,
    reviews: Iterable[dict[str, Any]] = (),
    threads: Iterable[dict[str, Any]] = (),
) -> list[FindingFeedback]:
    """One record per inline finding Argus posted, joined to its handoff record and thread.

    reviews are the pull requests' reviews (the handoffs are read from Argus's
    own); threads are {"comment_id", "resolved", "outdated"}, keyed by the
    thread's first comment.
    """
    if not reviewer_login:
        return []
    comments = list(comments)
    by_comment, by_finding = _handoff_records(reviews, reviewer_login)
    thread_of = {int(t["comment_id"]): t for t in threads}
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
        body = str(comment.get("body") or "")
        header = inline_header(body)
        if header is None:
            continue
        record = _record_for(comment, by_comment, by_finding)
        reactions = comment.get("reactions") or {}
        thread = thread_of.get(int(comment.get("id") or 0), {})
        line = comment.get("line") or comment.get("original_line")
        records.append(
            FindingFeedback(
                pr=pr_number_of(comment),
                url=str(comment.get("html_url") or ""),
                path=str(comment.get("path") or ""),
                line=int(line) if line else None,
                thumbs_up=int(reactions.get("+1") or 0),
                thumbs_down=int(reactions.get("-1") or 0),
                replies=replies.get(comment.get("id"), 0),
                specialist=reporter_of(comment, record) or header["category"],
                confidence=_confidence((record or {}).get("confidence")),
                resolved=bool(thread.get("resolved")),
                changed=bool(thread.get("outdated")),
                **header,
            )
        )
    return records


def pr_number_of(comment: dict[str, Any]) -> int | None:
    """The pull request an inline comment is on, read from its API URL."""
    match = _PR_NUMBER.search(str(comment.get("pull_request_url") or ""))
    return int(match.group(1)) if match else None


def reporter_of(comment: dict[str, Any], record: dict[str, Any] | None) -> str | None:
    """The specialist the handoff record names, else the agent the comment says found it."""
    named = (record or {}).get("specialist")
    return str(named) if named else found_by(str(comment.get("body") or ""))


def is_false_positive(thumbs_up: int, thumbs_down: int) -> bool:
    """The readers' verdict: more 👎 than 👍 marks a finding as a false positive."""
    return thumbs_down > thumbs_up


def handoff_record(
    comment: dict[str, Any], reviews: Iterable[dict[str, Any]], reviewer_login: str
) -> dict[str, Any] | None:
    """The handoff record of one inline finding comment, from Argus's own reviews."""
    return _record_for(comment, *_handoff_records(reviews, reviewer_login))


def _record_for(
    comment: dict[str, Any],
    by_comment: dict[Any, dict[str, Any]],
    by_finding: dict[tuple[Any, Any], dict[str, Any]],
) -> dict[str, Any] | None:
    """By the comment id written into the handoff, else by the finding id within its review.

    Finding ids restart in every review, so the second join is scoped to the
    review the comment belongs to.
    """
    body = str(comment.get("body") or "")
    return by_comment.get(comment.get("id")) or by_finding.get(
        (comment.get("pull_request_review_id"), finding_id_of(body))
    )


def _handoff_records(
    reviews: Iterable[dict[str, Any]], reviewer_login: str
) -> tuple[dict[Any, dict[str, Any]], dict[tuple[Any, Any], dict[str, Any]]]:
    """Argus's handoff records, by inline comment id and by (review id, finding id)."""
    by_comment: dict[Any, dict[str, Any]] = {}
    by_finding: dict[tuple[Any, Any], dict[str, Any]] = {}
    for review in reviews:
        if not is_argus_review(review, reviewer_login):
            continue
        payload = handoff_payload(str(review.get("body") or "")) or {}
        for record in payload.get("findings") or []:  # null: a failed run's review
            if not isinstance(record, dict):
                continue
            if record.get("comment") is not None:
                by_comment[record["comment"]] = record
            by_finding[(review.get("id"), record.get("id"))] = record
    return by_comment, by_finding


def _confidence(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def summarize_feedback(records: Iterable[FindingFeedback]) -> FeedbackSummary:
    records = list(records)
    by_severity = _tally(records, "severity")
    return FeedbackSummary(
        findings=len(records),
        acted_on=sum(r.acted_on for r in records),
        false_positives=sum(r.false_positive for r in records),
        by_specialist=_tally(records, "specialist"),
        by_severity=dict(
            sorted(
                by_severity.items(), key=lambda kv: SEVERITY_ORDER.get(kv[0], len(SEVERITY_ORDER))
            )
        ),
    )


def render_feedback(summary: FeedbackSummary) -> str:
    """Counts and rates only: no titles, paths, or links, so the table can be shared."""
    lines = [
        f"{summary.findings} findings posted inline, {summary.acted_on} acted on, "
        f"{summary.false_positives} marked false positive.",
        "",
    ]
    for title, groups in (("Specialist", summary.by_specialist), ("Severity", summary.by_severity)):
        lines += [
            f"| {title} | Posted | Acted on | False positives "
            "| Acted-on rate | False-positive rate |",
            "|---|---|---|---|---|---|",
        ]
        for name, tally in groups.items():
            lines.append(
                f"| {name} | {tally.posted} | {tally.acted_on} | {tally.false_positives} "
                f"| {_pct(tally.acted_on_rate)} | {_pct(tally.false_positive_rate)} |"
            )
        lines.append("")
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _tally(records: list[FindingFeedback], key: str) -> dict[str, Tally]:
    counts: dict[str, dict[str, int]] = {}
    for record in records:
        group = counts.setdefault(getattr(record, key) or "unknown", {})
        group["posted"] = group.get("posted", 0) + 1
        group["acted_on"] = group.get("acted_on", 0) + record.acted_on
        group["false_positives"] = group.get("false_positives", 0) + record.false_positive
    return {name: Tally(**group) for name, group in sorted(counts.items())}
