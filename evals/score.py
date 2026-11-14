"""Score a ReviewResult against a case's seeded bugs, as a whole and agent by agent.

A finding matches a seeded bug when it names the same file and the same
category, its line span comes within TOLERANCE lines of the bug's span, and
that span is at most MAX_SPAN lines wide, so a diffuse finding cannot claim a bug.
A finding is *reported* unless the verifier rejected it, the same rule the
report uses. Over the reported findings:

- a true positive is a seeded bug that at least one finding matches;
- a duplicate is a further finding on a bug that is already matched;
- a false positive is a finding that matches no seeded bug;
- a false negative is a seeded bug that no finding matches.

Recall is also kept per category, the specialist a bug is filed under, so a
new specialist's cases and the original correctness and security cases can
each be held to their own bar.

Each agent is then scored on its own output:

- a specialist, on the findings it recorded before the lead triaged them
  (Review.recorded, attributed by reported_by): its recall over the bugs it
  owns, and its precision. At this level a finding on a seeded bug's lines
  counts whatever category it was filed under: which agent saw the defect is
  what is scored;
- the lead, on what it did with them: seeded bugs a specialist recorded that
  the review lacks (lost), false findings it kept and removed, duplicates
  posted, and whether the nit cap and the findings cap held;
- the verifier, on its own answers (Verdict.verdict, before the posting rule
  turns a low-confidence confirmation into a rejection): a confusion matrix
  over seeded truth with the mean confidence in each cell. An inconclusive
  answer is not a decision and is counted on its own;
- verifier traps, plausible-looking non-bugs: how many a specialist flagged
  (triggered) and how many of those were still reported (posted).

Pure functions over domain types, with no SDK imports: the tests run it offline.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import get_args

from pydantic import BaseModel, ConfigDict, Field

from argus.domain.models import MAX_FINDINGS, Category, Finding, ReviewResult
from argus.domain.policy import BLOCKING_SEVERITIES, MAX_NITS
from evals.corpus import Case, CaseKind, ExpectedBug, Location, Trap

TOLERANCE = 3
"""Lines either side of a seeded bug's span that still count as pointing at it."""

MAX_SPAN = 15
"""The widest finding, in lines, that still localizes a bug: about one function."""

UNATTRIBUTED = "unattributed"
"""The reporter of a recorded finding no hook attributed to an agent."""

VERIFIER_CELLS = {
    "confirmed_true": "true_bugs_confirmed",
    "rejected_true": "true_bugs_rejected",
    "confirmed_false": "false_positives_confirmed",
    "rejected_false": "false_positives_rejected",
}
"""Each cell (the verifier's answer, then whether the finding was a seeded bug) and its count."""

CATEGORY_ORDER = list(get_args(Category))


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CaseScore(_Model):
    """One review of one case."""

    case: str
    clean: bool
    kind: CaseKind = "bug"
    trial: int = Field(default=1, ge=1, description="Which pass over the corpus this review was.")
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    duplicates: int = 0
    true_bugs_confirmed: int = 0
    true_bugs_rejected: int = 0
    false_positives_confirmed: int = 0
    false_positives_rejected: int = 0
    verifier_confidence: dict[str, float] = Field(default_factory=dict)
    """Summed confidence of the verifier's answers in each VERIFIER_CELLS cell."""
    verifier_inconclusive: int = 0
    caught: dict[str, int] = Field(default_factory=dict)
    """Seeded bugs found, by the category they are filed under."""
    missed: dict[str, int] = Field(default_factory=dict)
    specialist_caught: dict[str, int] = Field(default_factory=dict)
    """Seeded bugs the owning specialist recorded, by owner."""
    specialist_missed: dict[str, int] = Field(default_factory=dict)
    specialist_true: dict[str, int] = Field(default_factory=dict)
    """Recorded findings on a seeded bug, by the agent that recorded them."""
    specialist_false: dict[str, int] = Field(default_factory=dict)
    lead_lost: int = 0
    """Seeded bugs some specialist recorded that the review no longer holds."""
    lead_kept_false: int = 0
    lead_removed_false: int = 0
    nit_cap_held: bool = True
    max_findings_held: bool = True
    traps: int = 0
    traps_triggered: int = 0
    traps_posted: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    duration_ms: int = 0
    error: str | None = None
    true_positives_by_category: dict[str, int] = Field(default_factory=dict)
    false_positives_by_category: dict[str, int] = Field(default_factory=dict)

    @property
    def reported(self) -> int:
        return self.true_positives + self.duplicates + self.false_positives


class SpecialistScore(_Model):
    caught: int
    seeded: int
    recall: float | None
    true_findings: int
    false_findings: int
    precision: float | None


class LeadScore(_Model):
    lost: int
    duplicates: int
    kept_false: int
    removed_false: int
    nit_cap_breaches: int
    max_findings_breaches: int


class VerifierCell(_Model):
    count: int
    mean_confidence: float | None


class Summary(_Model):
    """Every case of one mode, pooled.

    The means use two populations on purpose: cost is averaged over every case,
    because a failed run still spent its quota, while turns and latency are
    averaged over the runs that completed, because a failed run reports neither.
    The clean-control rate also counts only the controls that completed: a
    crashed control reported nothing because it reviewed nothing, so it is an
    error, never a clean pass.
    """

    cases: int
    errors: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float | None
    recall: float | None
    clean_controls: int
    clean_controls_flagged: int
    clean_control_fp_rate: float | None
    true_bugs_confirmed: int
    true_bugs_rejected: int
    false_positives_confirmed: int
    false_positives_rejected: int
    verifier_inconclusive: int
    verifier_accuracy: float | None
    verifier_cells: dict[str, VerifierCell]
    caught_by_category: dict[str, int]
    seeded_by_category: dict[str, int]
    recall_by_category: dict[str, float]
    specialists: dict[str, SpecialistScore]
    lead: LeadScore
    traps: int
    traps_triggered: int
    traps_posted: int
    total_cost_usd: float
    mean_cost_usd: float | None
    mean_turns: float | None
    mean_duration_ms: float | None
    by_category: dict[str, dict[str, float | int | None]] = Field(default_factory=dict)
    """Reported findings per category: true_positives, false_positives, and precision.

    Duplicates count as neither. A category whose precision sits far below the
    rest is the specialist to tune, or to drop.
    """


def matches(
    finding: Finding,
    target: ExpectedBug | Trap,
    tolerance: int = TOLERANCE,
    *,
    any_category: bool = False,
) -> bool:
    """A narrow finding span within tolerance of any of the target's locations.

    The category must agree too, unless any_category: the specialist scores
    credit an agent for seeing a defect, whatever it filed it under.
    """
    if not any_category and finding.category != target.category:
        return False
    end = finding.end_line or finding.line
    if end - finding.line + 1 > MAX_SPAN:
        return False
    return any(
        finding.file == loc.file
        and finding.line <= loc.line_end + tolerance
        and end >= loc.line_start - tolerance
        for loc in _locations(target)
    )


def _locations(target: ExpectedBug | Trap) -> tuple[Location, ...]:
    return target.locations if isinstance(target, ExpectedBug) else (target,)


def score_case(case: Case, result: ReviewResult) -> CaseScore:
    findings = result.review.findings
    reported = [f for f in findings if f.status != "rejected"]
    matched: set[int] = set()
    tp = fp = duplicates = 0
    tp_by: Counter[str] = Counter()
    fp_by: Counter[str] = Counter()
    for finding in reported:
        hits = _hits(finding, case.expected)
        fresh = [i for i in hits if i not in matched]
        if fresh:
            matched.add(fresh[0])
            tp += 1
            tp_by[finding.category] += 1
        elif hits:
            duplicates += 1
        else:
            fp += 1
            fp_by[finding.category] += 1
    return CaseScore(
        case=case.name,
        clean=case.clean,
        kind=case.kind,
        caught=_by_category(bug for i, bug in enumerate(case.expected) if i in matched),
        missed=_by_category(bug for i, bug in enumerate(case.expected) if i not in matched),
        true_positives=tp,
        false_positives=fp,
        false_negatives=len(case.expected) - len(matched),
        duplicates=duplicates,
        cost_usd=result.total_cost_usd,
        turns=sum(m.num_turns for m in result.metrics),
        duration_ms=result.duration_ms,
        true_positives_by_category=dict(tp_by),
        false_positives_by_category=dict(fp_by),
        **_verifier_fields(case, result),
        **_specialist_fields(case, result.review.recorded),
        **_lead_fields(case, result),
        **_trap_fields(case, result),
    )


def _hits(finding: Finding, bugs: Sequence[ExpectedBug]) -> list[int]:
    return [i for i, bug in enumerate(bugs) if matches(finding, bug)]


def _on_its_lines(finding: Finding, bug: ExpectedBug) -> bool:
    """The agent-level match: on the bug's lines, whatever category it was filed under."""
    return matches(finding, bug, any_category=True)


def _on_a_bug(finding: Finding, bugs: Sequence[ExpectedBug]) -> bool:
    return any(_on_its_lines(finding, bug) for bug in bugs)


def _verifier_fields(case: Case, result: ReviewResult) -> dict[str, object]:
    by_id = {f.id: f for f in result.review.findings}
    counts: Counter[str] = Counter()
    confidence: Counter[str] = Counter()
    inconclusive = 0
    for verdict in result.verdicts:
        finding = by_id.get(verdict.finding_id)
        if finding is None:
            continue
        if verdict.verdict == "inconclusive":
            inconclusive += 1
            continue
        cell = f"{verdict.verdict}_{'true' if _hits(finding, case.expected) else 'false'}"
        counts[cell] += 1
        confidence[cell] += verdict.confidence
    return {
        **{field: counts[cell] for cell, field in VERIFIER_CELLS.items()},
        "verifier_confidence": dict(confidence),
        "verifier_inconclusive": inconclusive,
    }


def _specialist_fields(case: Case, recorded: list[Finding]) -> dict[str, object]:
    true_by: Counter[str] = Counter()
    false_by: Counter[str] = Counter()
    for finding in recorded:
        on_a_bug = _on_a_bug(finding, case.expected)
        (true_by if on_a_bug else false_by)[finding.reported_by or UNATTRIBUTED] += 1
    caught: Counter[str] = Counter()
    missed: Counter[str] = Counter()
    for bug in case.expected:
        found = any(f.reported_by == bug.owner and _on_its_lines(f, bug) for f in recorded)
        (caught if found else missed)[bug.owner] += 1
    return {
        "specialist_caught": dict(caught),
        "specialist_missed": dict(missed),
        "specialist_true": dict(true_by),
        "specialist_false": dict(false_by),
    }


def _lead_fields(case: Case, result: ReviewResult) -> dict[str, object]:
    """What the lead, and the curation policy after it, made of the specialists' findings.

    Judged on the review before the verifier, every status included: the
    verifier's rejections are its own score. Like the specialist scores,
    every count here ignores the category: keeping one of two findings on the
    same defect under the other's category is the lead's job, not a loss.
    """
    review = result.review.findings
    recorded = result.review.recorded

    lost = sum(
        any(_on_its_lines(f, bug) for f in recorded)
        and not any(_on_its_lines(f, bug) for f in review)
        for bug in case.expected
    )
    kept_false = sum(not _on_a_bug(f, case.expected) for f in review)
    recorded_false = sum(not _on_a_bug(f, case.expected) for f in recorded)
    severities = [f.severity for f in review]
    nits = severities.count("nit")
    blocking = any(s in BLOCKING_SEVERITIES for s in severities)
    return {
        "lead_lost": lost,
        "lead_kept_false": kept_false,
        "lead_removed_false": max(0, recorded_false - kept_false),
        "nit_cap_held": nits <= (0 if blocking else MAX_NITS),
        "max_findings_held": len(review) <= MAX_FINDINGS,
    }


def _trap_fields(case: Case, result: ReviewResult) -> dict[str, object]:
    recorded = result.review.recorded
    reported = [f for f in result.review.findings if f.status != "rejected"]
    triggered = [t for t in case.traps if any(matches(f, t) for f in recorded)]
    return {
        "traps": len(case.traps),
        "traps_triggered": len(triggered),
        "traps_posted": sum(any(matches(f, t) for f in reported) for t in triggered),
    }


def failed_case(case: Case, error: str, cost_usd: float) -> CaseScore:
    """A run that produced no result: every seeded bug is missed, and what it cost still counts."""
    return CaseScore(
        case=case.name,
        clean=case.clean,
        kind=case.kind,
        false_negatives=len(case.expected),
        missed=_by_category(case.expected),
        specialist_missed=dict(Counter(bug.owner for bug in case.expected)),
        traps=len(case.traps),
        cost_usd=cost_usd,
        error=error,
    )


def _by_category(bugs: Iterable[ExpectedBug]) -> dict[str, int]:
    return dict(Counter(bug.category for bug in bugs))


def ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def summarize(scores: Iterable[CaseScore]) -> Summary:
    scores = list(scores)
    tp = sum(s.true_positives for s in scores)
    fp = sum(s.false_positives for s in scores)
    fn = sum(s.false_negatives for s in scores)
    tc = sum(s.true_bugs_confirmed for s in scores)
    tr = sum(s.true_bugs_rejected for s in scores)
    fc = sum(s.false_positives_confirmed for s in scores)
    fr = sum(s.false_positives_rejected for s in scores)
    ran = [s for s in scores if s.error is None]
    controls = [s for s in ran if s.clean]
    flagged = sum(s.reported > 0 for s in controls)
    total_cost = round(sum(s.cost_usd for s in scores), 6)
    caught: Counter[str] = Counter()
    seeded: Counter[str] = Counter()
    for s in scores:
        caught.update(s.caught)
        seeded.update(s.caught)
        seeded.update(s.missed)
    tp_by: Counter[str] = Counter()
    fp_by: Counter[str] = Counter()
    for s in scores:
        tp_by.update(s.true_positives_by_category)
        fp_by.update(s.false_positives_by_category)
    by_category: dict[str, dict[str, float | int | None]] = {
        c: {
            "true_positives": tp_by[c],
            "false_positives": fp_by[c],
            "precision": ratio(tp_by[c], tp_by[c] + fp_by[c]),
        }
        for c in sorted(set(tp_by) | set(fp_by), key=CATEGORY_ORDER.index)
    }
    return Summary(
        cases=len(scores),
        errors=len(scores) - len(ran),
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=ratio(tp, tp + fp),
        recall=ratio(tp, tp + fn),
        clean_controls=len(controls),
        clean_controls_flagged=flagged,
        clean_control_fp_rate=ratio(flagged, len(controls)),
        true_bugs_confirmed=tc,
        true_bugs_rejected=tr,
        false_positives_confirmed=fc,
        false_positives_rejected=fr,
        verifier_inconclusive=sum(s.verifier_inconclusive for s in scores),
        verifier_accuracy=ratio(tc + fr, tc + tr + fc + fr),
        verifier_cells=_verifier_cells(scores),
        caught_by_category=dict(sorted(caught.items())),
        seeded_by_category=dict(sorted(seeded.items())),
        recall_by_category={c: caught[c] / n for c, n in sorted(seeded.items())},
        specialists=_specialists(scores),
        lead=LeadScore(
            lost=sum(s.lead_lost for s in scores),
            duplicates=sum(s.duplicates for s in scores),
            kept_false=sum(s.lead_kept_false for s in scores),
            removed_false=sum(s.lead_removed_false for s in scores),
            nit_cap_breaches=sum(not s.nit_cap_held for s in scores),
            max_findings_breaches=sum(not s.max_findings_held for s in scores),
        ),
        traps=sum(s.traps for s in scores),
        traps_triggered=sum(s.traps_triggered for s in scores),
        traps_posted=sum(s.traps_posted for s in scores),
        total_cost_usd=total_cost,
        mean_cost_usd=ratio(total_cost, len(scores)),
        mean_turns=ratio(sum(s.turns for s in ran), len(ran)),
        mean_duration_ms=ratio(sum(s.duration_ms for s in ran), len(ran)),
        by_category=by_category,
    )


def _verifier_cells(scores: list[CaseScore]) -> dict[str, VerifierCell]:
    counts = {
        cell: sum(getattr(s, field) for s in scores) for cell, field in VERIFIER_CELLS.items()
    }
    return {
        cell: VerifierCell(
            count=n,
            mean_confidence=ratio(sum(s.verifier_confidence.get(cell, 0.0) for s in scores), n),
        )
        for cell, n in counts.items()
    }


def _specialists(scores: list[CaseScore]) -> dict[str, SpecialistScore]:
    caught: Counter[str] = Counter()
    missed: Counter[str] = Counter()
    true_by: Counter[str] = Counter()
    false_by: Counter[str] = Counter()
    for s in scores:
        caught.update(s.specialist_caught)
        missed.update(s.specialist_missed)
        true_by.update(s.specialist_true)
        false_by.update(s.specialist_false)
    names = set(caught) | set(missed) | set(true_by) | set(false_by)
    order = [*CATEGORY_ORDER, UNATTRIBUTED]
    return {
        name: SpecialistScore(
            caught=caught[name],
            seeded=caught[name] + missed[name],
            recall=ratio(caught[name], caught[name] + missed[name]),
            true_findings=true_by[name],
            false_findings=false_by[name],
            precision=ratio(true_by[name], true_by[name] + false_by[name]),
        )
        for name in sorted(names, key=lambda n: order.index(n) if n in order else len(order))
    }
