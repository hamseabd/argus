import pytest

from argus.domain.models import Finding, Verdict
from argus.domain.policy import MAX_NITS, POST_CONFIDENCE, apply_verdict, curate_findings


def finding(fid: str, **overrides) -> Finding:
    base = {
        "id": fid,
        "file": "app/db.py",
        "line": 10,
        "severity": "medium",
        "category": "correctness",
        "title": f"title {fid}",
        "description": "d",
        "evidence": f"evidence {fid}",
        "confidence": 0.7,
    }
    return Finding(**{**base, **overrides})


def nits(n: int) -> list[Finding]:
    return [
        finding(f"maintainability-{i}", severity="nit", category="maintainability", line=100 + i)
        for i in range(1, n + 1)
    ]


def test_findings_at_different_locations_are_all_kept_in_order() -> None:
    findings = [finding("a", line=1), finding("b", line=2), finding("c", file="other.py")]

    assert [f.id for f in curate_findings(findings)] == ["a", "b", "c"]


def test_one_category_twice_at_the_same_location_becomes_the_more_severe_finding() -> None:
    findings = [
        finding("correctness-1", line=14, end_line=15, severity="medium"),
        finding("correctness-2", line=14, end_line=15, severity="high"),
    ]

    (kept,) = curate_findings(findings)

    assert kept.id == "correctness-2"
    assert kept.severity == "high"
    assert "evidence correctness-2" in kept.evidence
    assert "evidence correctness-1" in kept.evidence  # the evidence is the union


def test_two_categories_at_the_same_location_are_both_kept() -> None:
    """A test gap and the bug it would have caught share lines; merging would lose one."""
    findings = [
        finding("correctness-1", line=14, end_line=15, severity="high"),
        finding("tests-1", line=14, end_line=15, severity="medium", category="tests"),
    ]

    assert [f.id for f in curate_findings(findings)] == ["correctness-1", "tests-1"]


def test_a_severity_tie_at_the_same_location_keeps_the_more_confident_finding() -> None:
    findings = [finding("a", confidence=0.6), finding("b", confidence=0.9)]

    (kept,) = curate_findings(findings)

    assert kept.id == "b"


def test_a_single_line_finding_and_a_range_that_starts_there_are_not_merged() -> None:
    """Exact location only: an overlapping span can be a different defect, the lead's call."""
    findings = [finding("a", line=10), finding("b", line=10, end_line=14)]

    assert len(curate_findings(findings)) == 2


def test_identical_evidence_is_not_repeated_when_merged() -> None:
    findings = [finding("a", evidence="same"), finding("b", evidence="same", severity="low")]

    (kept,) = curate_findings(findings)

    assert kept.evidence == "same"


def test_nits_are_capped_at_five_keeping_the_most_confident() -> None:
    findings = [
        n.model_copy(update={"confidence": 0.5 + i / 100}) for i, n in enumerate(nits(7), start=1)
    ]

    kept = curate_findings(findings)

    assert MAX_NITS == 5
    assert [f.id for f in kept] == [f"maintainability-{i}" for i in range(3, 8)]


def test_nits_are_dropped_entirely_when_there_is_a_high_finding() -> None:
    findings = [finding("correctness-1", severity="high"), *nits(2)]

    assert [f.id for f in curate_findings(findings)] == ["correctness-1"]


def test_nits_are_dropped_entirely_when_there_is_a_critical_finding() -> None:
    findings = [*nits(1), finding("security-1", severity="critical", category="security")]

    assert [f.id for f in curate_findings(findings)] == ["security-1"]


def test_nits_stay_beside_medium_and_low_findings() -> None:
    findings = [finding("a", severity="medium"), finding("b", severity="low", line=2), *nits(1)]

    assert len(curate_findings(findings)) == 3


def test_a_nit_merged_into_a_high_finding_does_not_survive_as_a_nit() -> None:
    findings = [
        finding("maintainability-1", severity="nit", category="maintainability"),
        finding("correctness-1", severity="high"),
    ]

    (kept,) = curate_findings(findings)

    assert kept.id == "correctness-1"


def verdict(decision: str, confidence: float, severity: str = "medium") -> Verdict:
    return Verdict(
        finding_id="a", verdict=decision, reasoning="r", confidence=confidence, severity=severity
    )


def test_a_confirmation_at_the_threshold_is_posted() -> None:
    assert POST_CONFIDENCE == 0.6
    assert apply_verdict(finding("a"), verdict("confirmed", 0.6)).status == "confirmed"


def test_a_confirmation_below_the_threshold_is_not_posted() -> None:
    assert apply_verdict(finding("a"), verdict("confirmed", 0.59)).status == "rejected"


def test_a_rejection_is_never_posted_whatever_its_confidence() -> None:
    assert apply_verdict(finding("a"), verdict("rejected", 0.95)).status == "rejected"


@pytest.mark.parametrize(
    ("reported", "judged", "final"),
    [
        ("medium", "high", "high"),  # one step up
        ("medium", "low", "low"),  # one step down
        ("low", "critical", "medium"),  # at most one step up
        ("high", "nit", "medium"),  # at most one step down
        ("nit", "nit", "nit"),
    ],
)
def test_the_verifier_moves_severity_at_most_one_step(
    reported: str, judged: str, final: str
) -> None:
    confirmed = apply_verdict(finding("a", severity=reported), verdict("confirmed", 0.9, judged))

    assert confirmed.severity == final


def test_a_rejected_finding_keeps_the_severity_it_was_reported_with() -> None:
    rejected = apply_verdict(finding("a", severity="high"), verdict("rejected", 0.9, "low"))

    assert rejected.severity == "high"


def test_an_inconclusive_verdict_leaves_the_finding_unverified() -> None:
    finding_ = finding("a", severity="high")

    judged = apply_verdict(finding_, verdict("inconclusive", 0.9, "low"))

    assert (judged.status, judged.severity) == ("unverified", "high")
