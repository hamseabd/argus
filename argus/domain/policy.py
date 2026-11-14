"""Review policy: which findings go on to verification, and which get posted.

The lead merges what six specialists report and is told to apply the
curation rules itself; curate_findings enforces the mechanical part so a
lapse costs neither a verifier query nor a reader's attention. Duplicates
are merged only within one category and at the exact same location: an
overlapping span, or another specialist's finding on the same lines (a test
gap beside the bug it would have caught), can be a different defect, and
telling those apart is the lead's judgement.

apply_verdict is the posting rule: a finding is posted only when the
verifier confirmed it with confidence at or above POST_CONFIDENCE.
"""

from typing import get_args

from argus.domain.models import SEVERITY_ORDER, Finding, Severity, Verdict

MAX_NITS = 5
POST_CONFIDENCE = 0.6
_SEVERITIES: tuple[Severity, ...] = get_args(Severity)
"""Most to least severe, the order SEVERITY_ORDER numbers them in."""
BLOCKING_SEVERITIES = frozenset({"critical", "high"})
"""With any of these in a review, nits are noise and are dropped."""


def curate_findings(findings: list[Finding]) -> list[Finding]:
    """Merge same-location duplicates, then apply the nit policy, keeping the lead's order."""
    merged = _merge_duplicates(findings)
    if any(f.severity in BLOCKING_SEVERITIES for f in merged):
        return [f for f in merged if f.severity != "nit"]
    nits = [f for f in merged if f.severity == "nit"]
    kept = {f.id for f in sorted(nits, key=lambda f: -f.confidence)[:MAX_NITS]}
    return [f for f in merged if f.severity != "nit" or f.id in kept]


def _merge_duplicates(findings: list[Finding]) -> list[Finding]:
    by_location: dict[tuple[str, str, int, int], Finding] = {}
    for finding in findings:
        key = (finding.category, finding.file, finding.line, finding.end_line or finding.line)
        if (seen := by_location.get(key)) is None:
            by_location[key] = finding
        else:
            by_location[key] = _merge(seen, finding)
    return list(by_location.values())


def _merge(a: Finding, b: Finding) -> Finding:
    """The stronger finding, carrying the other's evidence too."""
    keep, other = (a, b) if _strength(a) >= _strength(b) else (b, a)
    if other.evidence.strip() in keep.evidence:
        return keep
    return keep.model_copy(update={"evidence": f"{keep.evidence}\n\n{other.evidence}"})


def _strength(finding: Finding) -> tuple[int, float]:
    return (-SEVERITY_ORDER[finding.severity], finding.confidence)


def apply_verdict(finding: Finding, verdict: Verdict) -> Finding:
    """The finding with its final status and, when confirmed, its final severity.

    An inconclusive verdict leaves the finding unverified: reported in the
    review body, never inline. A confirmation below POST_CONFIDENCE is not
    posted, so it counts as rejected. The verifier may move the severity, but
    by one step at most.
    """
    if verdict.verdict == "inconclusive":
        return finding.with_status("unverified")
    if verdict.verdict != "confirmed" or verdict.confidence < POST_CONFIDENCE:
        return finding.with_status("rejected")
    reported = SEVERITY_ORDER[finding.severity]
    judged = SEVERITY_ORDER[verdict.severity]
    step = max(-1, min(1, judged - reported))
    return finding.model_copy(
        update={"status": "confirmed", "severity": _SEVERITIES[reported + step]}
    )
