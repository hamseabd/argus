"""The merge gate as a GitHub check run.

The CLI's exit code gates a merge only by failing the job, which reads the
same as Argus crashing. A check run named "Argus" says it precisely: its
conclusion is failure when a reported finding is at or above the --fail-on
severity, neutral when there are findings below it, success when there are
none, and each reported finding is an annotation on its lines. A branch
protection rule that requires the "Argus" check then gates the merge.

Known findings are not annotated again, but they still count toward the
gate: reported before is not fixed.
"""

from typing import Any

from argus.domain.models import (
    SEVERITY_ORDER,
    Finding,
    ReviewContext,
    ReviewResult,
    gate_tripped,
    rank_findings,
)
from argus.report.markdown import render_counts

CHECK_NAME = "Argus"
MAX_ANNOTATIONS = 50
"""Annotations the API takes in one request; a review never holds more findings than this."""
_MAX_TEXT = 65_000


def build_check_run(
    result: ReviewResult, context: ReviewContext, fail_on: str | None
) -> dict[str, Any]:
    """The check-run payload for the pull request's head."""
    if context.pr is None:
        raise ValueError("a check run needs a pull request context")
    findings = result.review.findings
    tripped = fail_on is not None and gate_tripped(findings, fail_on)
    reported = rank_findings(findings)
    if tripped:
        conclusion = "failure"
    elif any(f.status != "rejected" for f in findings):
        conclusion = "neutral"
    else:
        conclusion = "success"
    annotations = [
        _annotation(f, fail_on) for f in reported if f.status in ("confirmed", "unverified")
    ]
    counts = render_counts(findings)
    return {
        "name": CHECK_NAME,
        "head_sha": context.pr.head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "output": {
            "title": counts,
            "summary": (result.review.summary.strip() + "\n\n" + counts)[:_MAX_TEXT],
            "annotations": annotations,
        },
    }


def _at_or_above(finding: Finding, fail_on: str) -> bool:
    return SEVERITY_ORDER[finding.severity] <= SEVERITY_ORDER[fail_on]


def _annotation(finding: Finding, fail_on: str | None) -> dict[str, Any]:
    level = "failure" if fail_on is not None and _at_or_above(finding, fail_on) else "warning"
    return {
        "path": finding.file,
        "start_line": finding.line,
        "end_line": finding.end_line or finding.line,
        "annotation_level": level,
        "title": f"[{finding.severity.upper()}] {finding.title}",
        "message": (
            f"{finding.description.strip()}\n\nEvidence: {finding.evidence.strip()}\n\n"
            f"{finding.category} · {finding.status} · confidence {finding.confidence:.2f}"
        )[:_MAX_TEXT],
    }
