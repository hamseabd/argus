"""Score a ReviewResult against a case's seeded bugs.

A finding matches a seeded bug when it names the same file and the same
category, its line span comes within TOLERANCE lines of the bug's span, and
that span is at most MAX_SPAN lines wide, so a diffuse finding cannot claim a bug.
A finding is *reported* unless the verifier rejected it, the same rule the
report uses. Over the reported findings:

- a true positive is a seeded bug that at least one finding matches;
- a duplicate is a further finding on a bug that is already matched;
- a false positive is a finding that matches no seeded bug;
- a false negative is a seeded bug that no finding matches.

The verifier is scored separately, on every finding it decided (confirmed or
rejected; a failed verification is not a decision): confirming a real bug or
rejecting a false one is right, the other two are wrong.

Pure functions over domain types, with no SDK imports: the tests run it offline.
"""

from collections import Counter
from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ConfigDict

from argus.domain.models import Finding, ReviewResult
from evals.corpus import Case, ExpectedBug

TOLERANCE = 3
"""Lines either side of a seeded bug's span that still count as pointing at it."""

MAX_SPAN = 15
"""The widest finding, in lines, that still localizes a bug: about one function."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CaseScore(_Model):
    """One review of one case."""

    case: str
    clean: bool
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    duplicates: int = 0
    true_bugs_confirmed: int = 0
    true_bugs_rejected: int = 0
    false_positives_confirmed: int = 0
    false_positives_rejected: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    duration_ms: int = 0
    error: str | None = None

    @property
    def reported(self) -> int:
        return self.true_positives + self.duplicates + self.false_positives


class Summary(_Model):
    """Every case of one mode, pooled.

    The means use two populations on purpose: cost is averaged over every case,
    because a failed run still spent its quota, while turns and latency are
    averaged over the runs that completed, because a failed run reports neither.
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
    verifier_accuracy: float | None
    total_cost_usd: float
    mean_cost_usd: float | None
    mean_turns: float | None
    mean_duration_ms: float | None


def matches(finding: Finding, bug: ExpectedBug, tolerance: int = TOLERANCE) -> bool:
    """Same file, same category, and a narrow finding span within tolerance of the bug's."""
    if finding.file != bug.file or finding.category != bug.category:
        return False
    end = finding.end_line or finding.line
    if end - finding.line + 1 > MAX_SPAN:
        return False
    return finding.line <= bug.line_end + tolerance and end >= bug.line_start - tolerance


def score_case(case: Case, result: ReviewResult) -> CaseScore:
    matched: set[int] = set()
    tp = fp = duplicates = 0
    verdicts: Counter[tuple[bool, str]] = Counter()  # (real bug, verifier decision)
    for finding in result.review.findings:
        hits = [i for i, bug in enumerate(case.expected) if matches(finding, bug)]
        if finding.status in ("confirmed", "rejected"):
            verdicts[(bool(hits), finding.status)] += 1
        if finding.status == "rejected":
            continue
        fresh = [i for i in hits if i not in matched]
        if fresh:
            matched.add(fresh[0])
            tp += 1
        elif hits:
            duplicates += 1
        else:
            fp += 1
    return CaseScore(
        case=case.name,
        clean=case.clean,
        true_positives=tp,
        false_positives=fp,
        false_negatives=len(case.expected) - len(matched),
        duplicates=duplicates,
        true_bugs_confirmed=verdicts[(True, "confirmed")],
        true_bugs_rejected=verdicts[(True, "rejected")],
        false_positives_confirmed=verdicts[(False, "confirmed")],
        false_positives_rejected=verdicts[(False, "rejected")],
        cost_usd=result.total_cost_usd,
        turns=sum(m.num_turns for m in result.metrics),
        duration_ms=result.duration_ms,
    )


def failed_case(case: Case, error: str, cost_usd: float) -> CaseScore:
    """A run that produced no result: every seeded bug is missed, and what it cost still counts."""
    return CaseScore(
        case=case.name,
        clean=case.clean,
        false_negatives=len(case.expected),
        cost_usd=cost_usd,
        error=error,
    )


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def summarize(scores: Iterable[CaseScore]) -> Summary:
    scores = list(scores)
    tp = sum(s.true_positives for s in scores)
    fp = sum(s.false_positives for s in scores)
    fn = sum(s.false_negatives for s in scores)
    controls = [s for s in scores if s.clean]
    flagged = sum(s.reported > 0 for s in controls)
    tc = sum(s.true_bugs_confirmed for s in scores)
    tr = sum(s.true_bugs_rejected for s in scores)
    fc = sum(s.false_positives_confirmed for s in scores)
    fr = sum(s.false_positives_rejected for s in scores)
    ran = [s for s in scores if s.error is None]
    total_cost = round(sum(s.cost_usd for s in scores), 6)
    return Summary(
        cases=len(scores),
        errors=len(scores) - len(ran),
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=_ratio(tp, tp + fp),
        recall=_ratio(tp, tp + fn),
        clean_controls=len(controls),
        clean_controls_flagged=flagged,
        clean_control_fp_rate=_ratio(flagged, len(controls)),
        true_bugs_confirmed=tc,
        true_bugs_rejected=tr,
        false_positives_confirmed=fc,
        false_positives_rejected=fr,
        verifier_accuracy=_ratio(tc + fr, tc + tr + fc + fr),
        total_cost_usd=total_cost,
        mean_cost_usd=_ratio(total_cost, len(scores)),
        mean_turns=_ratio(sum(s.turns for s in ran), len(ran)),
        mean_duration_ms=_ratio(sum(s.duration_ms for s in ran), len(ran)),
    )


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _num(value: float | None, fmt: str) -> str:
    return "n/a" if value is None else format(value, fmt)


def render_markdown(scores_by_mode: Mapping[str, list[CaseScore]]) -> str:
    """A summary row per mode, then a row per case with its counts in every mode."""
    lines = [
        "| Mode | Precision | Recall | Clean-control FP rate | Verifier accuracy | TP / FP / FN "
        "| Cost per case | Turns per completed review | Latency per completed review | Errors |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for mode, scores in scores_by_mode.items():
        s = summarize(scores)
        latency = None if s.mean_duration_ms is None else s.mean_duration_ms / 1000
        lines.append(
            f"| {mode} | {_pct(s.precision)} | {_pct(s.recall)} "
            f"| {_pct(s.clean_control_fp_rate)} ({s.clean_controls_flagged}/{s.clean_controls}) "
            f"| {_pct(s.verifier_accuracy)} "
            f"| {s.true_positives} / {s.false_positives} / {s.false_negatives} "
            f"| ${_num(s.mean_cost_usd, '.2f')} | {_num(s.mean_turns, '.1f')} "
            f"| {_num(latency, '.0f')} s | {s.errors} |"
        )
    modes = list(scores_by_mode)
    by_case: dict[str, dict[str, CaseScore]] = {}
    for mode, scores in scores_by_mode.items():
        for score in scores:
            by_case.setdefault(score.case, {})[mode] = score
    lines += [
        "",
        "| Case | " + " | ".join(f"{m}: TP / FP / FN" for m in modes) + " |",
        "|---|" + "---|" * len(modes),
    ]
    for case, per_mode in sorted(by_case.items()):
        cells = []
        for mode in modes:
            s = per_mode.get(mode)
            if s is None:
                cells.append("")
            elif s.error is not None:
                cells.append("error")
            else:
                cells.append(f"{s.true_positives} / {s.false_positives} / {s.false_negatives}")
        lines.append(f"| {case} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
