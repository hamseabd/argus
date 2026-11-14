"""Scoring a review against a case's expected bugs, offline."""

from pathlib import Path

import pytest

from argus.domain.models import Finding, Review, ReviewResult, StageMetrics, Verdict
from evals.corpus import Case, ExpectedBug, Trap
from evals.report import render_markdown
from evals.score import (
    MAX_SPAN,
    CaseScore,
    failed_case,
    matches,
    score_case,
    summarize,
)

SQLI = ExpectedBug(
    file="app/customers.py", line_start=14, line_end=15, category="security", severity="high"
)


def bug_case(*expected: ExpectedBug, name: str = "sqli") -> Case:
    return Case(name=name, path=Path("/cases") / name, expected=expected)


def clean_case(name: str = "clean_docs_only") -> Case:
    return Case(name=name, path=Path("/cases") / name, expected=(), kind="clean")


def finding(
    fid: str,
    *,
    file: str = "app/customers.py",
    line: int = 14,
    end_line: int | None = None,
    category: str = "security",
    status: str = "confirmed",
    severity: str = "high",
    reported_by: str | None = None,
) -> Finding:
    return Finding(
        id=fid,
        file=file,
        line=line,
        end_line=end_line,
        severity=severity,
        category=category,
        title="t",
        description="d",
        evidence="e",
        confidence=0.9,
        status=status,
        reported_by=reported_by,
    )


def verdict(fid: str, decision: str, confidence: float) -> Verdict:
    return Verdict(
        finding_id=fid, verdict=decision, reasoning="r", severity="high", confidence=confidence
    )


def verdicts_for(findings: tuple[Finding, ...]) -> list[Verdict]:
    """The verifier's answers that would have given each finding its status."""
    decisions = {"confirmed": ("confirmed", 0.9), "rejected": ("rejected", 0.8)}
    return [verdict(f.id, *decisions[f.status]) for f in findings if f.status in decisions]


def result(
    *findings: Finding,
    cost: float = 1.0,
    turns=(5, 2),
    duration_ms: int = 9000,
    recorded: tuple[Finding, ...] = (),
    verdicts: list[Verdict] | None = None,
):
    metrics = [
        StageMetrics(
            stage="review" if i == 0 else f"verify:{i}",
            model="m",
            cost_usd=cost / len(turns),
            input_tokens=0,
            output_tokens=0,
            num_turns=n,
            duration_ms=0,
        )
        for i, n in enumerate(turns)
    ]
    return ReviewResult(
        review=Review(
            summary="s", findings=list(findings), files_reviewed=[], recorded=list(recorded)
        ),
        verdicts=verdicts_for(findings) if verdicts is None else verdicts,
        metrics=metrics,
        total_cost_usd=cost,
        duration_ms=duration_ms,
        session_id="x",
    )


# --- matching ---------------------------------------------------------------


@pytest.mark.parametrize("line", [11, 14, 15, 18])
def test_a_finding_within_three_lines_of_the_bug_matches(line: int) -> None:
    assert matches(finding("f", line=line), SQLI)


@pytest.mark.parametrize("line", [10, 19])
def test_a_finding_more_than_three_lines_away_does_not_match(line: int) -> None:
    assert not matches(finding("f", line=line), SQLI)


def test_a_finding_whose_span_reaches_the_bug_matches() -> None:
    assert matches(finding("f", line=2, end_line=12), SQLI)


def test_a_finding_as_wide_as_the_span_limit_still_matches() -> None:
    assert matches(finding("f", line=1, end_line=MAX_SPAN), SQLI)


def test_a_diffuse_finding_wider_than_the_span_limit_does_not_match() -> None:
    assert MAX_SPAN == 15
    assert not matches(finding("f", line=1, end_line=MAX_SPAN + 1), SQLI)
    assert not matches(finding("f", line=1, end_line=200), SQLI)


def test_a_finding_in_another_file_does_not_match() -> None:
    assert not matches(finding("f", file="app/other.py"), SQLI)


def test_a_finding_in_another_category_does_not_match() -> None:
    assert not matches(finding("f", category="correctness"), SQLI)


# --- one case ---------------------------------------------------------------


def test_a_confirmed_finding_on_the_bug_is_a_true_positive() -> None:
    score = score_case(bug_case(SQLI), result(finding("s-1")))

    assert (score.true_positives, score.false_positives, score.false_negatives) == (1, 0, 0)


def test_a_missed_bug_is_a_false_negative_and_a_stray_finding_a_false_positive() -> None:
    score = score_case(bug_case(SQLI), result(finding("s-1", line=40)))

    assert (score.true_positives, score.false_positives, score.false_negatives) == (0, 1, 1)


def test_an_unverified_finding_counts_as_reported() -> None:
    score = score_case(bug_case(SQLI), result(finding("s-1", status="unverified")))

    assert score.true_positives == 1


def test_a_rejected_finding_is_not_reported() -> None:
    score = score_case(bug_case(SQLI), result(finding("s-1", status="rejected")))

    assert (score.true_positives, score.false_positives, score.false_negatives) == (0, 0, 1)


def test_a_second_finding_on_the_same_bug_is_a_duplicate_not_a_false_positive() -> None:
    score = score_case(bug_case(SQLI), result(finding("s-1"), finding("s-2", line=15)))

    assert (score.true_positives, score.false_positives, score.duplicates) == (1, 0, 1)


def test_the_verifier_is_scored_on_what_it_rejected() -> None:
    review = result(
        finding("true-kept"),
        finding("true-dropped", line=15, status="rejected"),
        finding("fp-dropped", line=40, status="rejected"),
        finding("fp-kept", line=50),
        finding("fp-unverified", line=60, status="unverified"),
    )

    score = score_case(bug_case(SQLI), review)

    assert score.true_bugs_confirmed == 1
    assert score.true_bugs_rejected == 1
    assert score.false_positives_rejected == 1
    assert score.false_positives_confirmed == 1  # unverified is not a verifier decision


def test_a_case_carries_the_run_cost_turns_and_latency() -> None:
    score = score_case(bug_case(SQLI), result(cost=1.5, turns=(7, 3, 2), duration_ms=4200))

    assert (score.cost_usd, score.turns, score.duration_ms) == (1.5, 12, 4200)
    assert score.error is None


def test_a_failed_run_misses_every_bug_and_keeps_its_cost() -> None:
    score = failed_case(bug_case(SQLI), "review: no output", cost_usd=0.4)

    assert (score.true_positives, score.false_negatives) == (0, 1)
    assert score.cost_usd == 0.4
    assert score.error == "review: no output"


# --- across cases -----------------------------------------------------------


def test_precision_and_recall_are_pooled_over_every_case() -> None:
    scores = [
        score_case(bug_case(SQLI), result(finding("a"), finding("b", line=40))),  # TP, FP
        score_case(bug_case(SQLI, name="x"), result()),  # FN
        score_case(clean_case(), result(finding("c", line=3))),  # FP on a control
    ]

    summary = summarize(scores)

    assert (summary.true_positives, summary.false_positives, summary.false_negatives) == (1, 2, 1)
    assert summary.precision == pytest.approx(1 / 3)
    assert summary.recall == pytest.approx(1 / 2)


def test_the_clean_control_rate_is_the_share_of_controls_with_any_reported_finding() -> None:
    scores = [
        score_case(clean_case("clean_a"), result(finding("a"), finding("b", line=40))),
        score_case(clean_case("clean_b"), result(finding("c", status="rejected"))),
        score_case(clean_case("clean_c"), result()),
        score_case(bug_case(SQLI), result(finding("d", line=40))),  # not a control
    ]

    summary = summarize(scores)

    assert (summary.clean_controls, summary.clean_controls_flagged) == (3, 1)
    assert summary.clean_control_fp_rate == pytest.approx(1 / 3)


def test_verifier_accuracy_is_right_decisions_over_all_decisions() -> None:
    scores = [
        score_case(
            bug_case(SQLI),
            result(
                finding("true-kept"),
                finding("fp-dropped", line=40, status="rejected"),
                finding("fp-kept", line=50),
                finding("true-dropped", line=15, status="rejected"),
            ),
        )
    ]

    summary = summarize(scores)

    assert summary.verifier_accuracy == pytest.approx(2 / 4)
    assert (summary.true_bugs_rejected, summary.false_positives_rejected) == (1, 1)


def test_rates_with_nothing_to_divide_are_none_not_zero() -> None:
    summary = summarize([score_case(clean_case(), result())])

    assert summary.precision is None
    assert summary.recall is None
    assert summary.verifier_accuracy is None
    assert summary.clean_control_fp_rate == 0.0


def test_cost_turns_and_latency_are_totalled_and_averaged_per_review() -> None:
    scores = [
        score_case(bug_case(SQLI), result(cost=1.0, turns=(4,), duration_ms=1000)),
        score_case(clean_case(), result(cost=3.0, turns=(8,), duration_ms=3000)),
    ]

    summary = summarize(scores)

    assert summary.total_cost_usd == pytest.approx(4.0)
    assert summary.mean_cost_usd == pytest.approx(2.0)
    assert summary.mean_turns == pytest.approx(6.0)
    assert summary.mean_duration_ms == pytest.approx(2000)


def test_cost_averages_over_every_case_and_turns_and_latency_over_completed_runs() -> None:
    scores = [
        failed_case(bug_case(SQLI), "boom", cost_usd=3.0),
        score_case(clean_case(), result(cost=1.0, turns=(4,), duration_ms=2000)),
    ]

    summary = summarize(scores)

    assert summary.mean_cost_usd == pytest.approx(2.0)  # a failed run still spent its quota
    assert summary.mean_turns == pytest.approx(4.0)  # a failed run has no turns to count
    assert summary.mean_duration_ms == pytest.approx(2000)
    header = render_markdown({"verify": scores}).splitlines()[0]
    assert "| Cost per case |" in header
    assert "| Turns per completed review |" in header
    assert "| Latency per completed review |" in header


def test_failed_runs_are_counted() -> None:
    summary = summarize([failed_case(bug_case(SQLI), "boom", cost_usd=0.0)])

    assert summary.errors == 1
    assert summary.false_negatives == 1


def test_the_markdown_has_one_row_per_mode_and_one_per_case() -> None:
    scores = {
        "verify": [score_case(bug_case(SQLI), result(finding("a")))],
        "no-verify": [score_case(bug_case(SQLI), result(finding("a", status="unverified")))],
    }

    table = render_markdown(scores)

    lines = table.splitlines()
    summary_table = lines[: lines.index("")]
    assert lines[0].startswith("| Mode | Precision | Recall |")
    assert sum(line.startswith("| verify |") for line in summary_table) == 1
    assert sum(line.startswith("| no-verify |") for line in summary_table) == 1
    assert sum(line.startswith("| sqli |") for line in lines) == 1  # per-case table
    assert "100%" in table
    assert "n/a" in table  # no-verify makes no verifier decisions


def test_a_case_score_round_trips_through_json() -> None:
    score = score_case(bug_case(SQLI), result(finding("a")))

    assert CaseScore.model_validate_json(score.model_dump_json()) == score


def test_a_crashed_clean_control_is_an_error_not_a_clean_pass() -> None:
    scores = [
        failed_case(clean_case("clean_a"), "boom", cost_usd=0.5),
        score_case(clean_case("clean_b"), result(finding("a"))),
        score_case(clean_case("clean_c"), result()),
    ]

    summary = summarize(scores)

    assert summary.errors == 1
    assert (summary.clean_controls, summary.clean_controls_flagged) == (2, 1)
    assert summary.clean_control_fp_rate == pytest.approx(1 / 2)


def test_only_crashed_clean_controls_leave_the_rate_undefined() -> None:
    summary = summarize([failed_case(clean_case(), "boom", cost_usd=0.0)])

    assert summary.clean_controls == 0
    assert summary.clean_control_fp_rate is None


COMMENT_BUG = ExpectedBug(
    file="app/fetch.py", line_start=6, line_end=6, category="maintainability", severity="low"
)


def test_each_case_counts_its_bugs_caught_and_missed_by_category() -> None:
    caught = score_case(bug_case(SQLI), result(finding("a")))
    missed = score_case(bug_case(COMMENT_BUG, name="comment"), result())

    assert (caught.caught, caught.missed) == ({"security": 1}, {})
    assert (missed.caught, missed.missed) == ({}, {"maintainability": 1})


def test_a_failed_run_misses_every_bug_in_its_category() -> None:
    assert failed_case(bug_case(SQLI), "boom", cost_usd=0.0).missed == {"security": 1}


def test_recall_is_pooled_per_category_in_the_summary_and_the_table() -> None:
    scores = [
        score_case(bug_case(SQLI), result(finding("a"))),
        score_case(bug_case(SQLI, name="sqli_again"), result()),
        score_case(bug_case(COMMENT_BUG, name="comment"), result()),
    ]

    summary = summarize(scores)
    table = render_markdown({"verify": scores})

    assert summary.recall_by_category == {"maintainability": 0.0, "security": 0.5}
    assert "| Category | Mode | Caught / seeded | Recall |" in table
    assert "| security | verify | 1 / 2 | 50% |" in table
    assert "| maintainability | verify | 0 / 1 | 0% |" in table


# --- alternate locations, categories, trials -------------------------------

CONTRACT = ExpectedBug(
    file="app/accounts.py",
    line_start=10,
    line_end=12,
    category="correctness",
    severity="high",
    also=[{"file": "app/billing.py", "line_start": 10, "line_end": 10}],
)


def test_a_finding_at_an_alternate_location_matches_the_bug() -> None:
    # A broken contract can be reported where it changed or where a caller breaks.
    at_caller = finding("c", file="app/billing.py", line=10, category="correctness")
    at_change = finding("d", file="app/accounts.py", line=11, category="correctness")

    assert matches(at_caller, CONTRACT)
    assert matches(at_change, CONTRACT)
    assert not matches(finding("e", file="app/store.py", line=10, category="correctness"), CONTRACT)


def test_both_locations_of_one_bug_count_once_and_the_other_as_a_duplicate() -> None:
    case = bug_case(CONTRACT, name="cross_file_contract")
    score = score_case(
        case,
        result(
            finding("a", file="app/accounts.py", line=11, category="correctness"),
            finding("b", file="app/billing.py", line=10, category="correctness"),
        ),
    )

    assert (score.true_positives, score.duplicates, score.false_positives) == (1, 1, 0)


def test_reported_findings_are_counted_by_category() -> None:
    score = score_case(
        bug_case(SQLI),
        result(
            finding("a"),
            finding("b", file="app/other.py", category="maintainability"),
            finding("c", file="app/other.py", category="maintainability"),
            finding("d", file="app/other.py", category="correctness", status="rejected"),
        ),
    )

    assert score.true_positives_by_category == {"security": 1}
    assert score.false_positives_by_category == {"maintainability": 2}


def test_category_counts_are_pooled_into_the_summary_and_the_markdown() -> None:
    a = score_case(
        bug_case(SQLI), result(finding("a"), finding("b", file="x.py", category="maintainability"))
    )
    b = score_case(bug_case(SQLI), result(finding("c", file="y.py", category="maintainability")))

    summary = summarize([a, b])

    assert summary.by_category == {
        "security": {"true_positives": 1, "false_positives": 0, "precision": 1.0},
        "maintainability": {"true_positives": 0, "false_positives": 2, "precision": 0.0},
    }
    table = render_markdown({"verify": [a, b]})
    assert "| maintainability | 0 / 2 (0%) |" in table


def test_with_several_trials_the_markdown_shows_each_trials_recall_and_precision() -> None:
    hit = score_case(bug_case(SQLI), result(finding("a")))
    miss = score_case(bug_case(SQLI), result())
    scores = [
        hit.model_copy(update={"trial": 1}),
        miss.model_copy(update={"trial": 2}),
        hit.model_copy(update={"trial": 3}),
    ]

    table = render_markdown({"verify": scores})

    assert "| verify | 100% | 67% |" in table  # pooled over every trial
    assert "verify recall per trial: 100%, 0%, 100%" in table
    assert "verify precision per trial: 100%, n/a, 100%" in table


def test_a_single_trial_adds_no_per_trial_line() -> None:
    table = render_markdown({"verify": [score_case(bug_case(SQLI), result(finding("a")))]})

    assert "per trial" not in table


def test_inconclusive_verifications_are_counted_but_are_not_decisions() -> None:
    unsure = finding("a", status="unverified")
    scored = score_case(
        bug_case(SQLI), result(unsure, verdicts=[verdict("a", "inconclusive", 0.2)])
    )

    assert scored.verifier_inconclusive == 1
    assert (scored.true_bugs_confirmed, scored.true_bugs_rejected) == (0, 0)
    assert summarize([scored]).verifier_inconclusive == 1


def test_the_table_shows_how_often_the_verifier_was_inconclusive() -> None:
    unsure = score_case(bug_case(SQLI), result(finding("a", status="unverified")))
    unsure = unsure.model_copy(update={"verifier_inconclusive": 2})

    assert "(2 inconclusive)" in render_markdown({"verify": [unsure]})
    assert "inconclusive" not in render_markdown({"verify": [score_case(bug_case(SQLI), result())]})


def test_a_case_that_errored_on_some_trials_sums_the_rest_and_counts_the_errors() -> None:
    ok = score_case(bug_case(SQLI), result(finding("a")))
    broken = failed_case(bug_case(SQLI), "boom", 0.1).model_copy(update={"trial": 2})

    table = render_markdown({"verify": [ok, broken]})

    assert "| sqli | 1 / 0 / 1 (1 errored) |" in table


# --- per agent --------------------------------------------------------------


def recorded(fid: str, reporter: str | None, **kwargs) -> Finding:
    return finding(fid, status="pending", reported_by=reporter, **kwargs)


def test_each_specialist_is_scored_on_what_it_recorded_before_the_lead() -> None:
    raw = (
        recorded("F1", "security"),
        recorded("F2", "security", line=40),
        recorded("F3", "correctness", category="correctness"),  # on the bug's lines
    )

    score = score_case(bug_case(SQLI), result(recorded=raw))

    assert (score.specialist_caught, score.specialist_missed) == ({"security": 1}, {})
    assert score.specialist_true == {"security": 1, "correctness": 1}
    assert score.specialist_false == {"security": 1}


def test_a_bug_only_another_specialist_found_is_a_miss_for_its_owner() -> None:
    raw = (recorded("F1", "correctness", category="correctness"),)

    score = score_case(bug_case(SQLI), result(recorded=raw))

    assert score.specialist_missed == {"security": 1}


def test_a_named_specialist_owns_the_bug_whatever_its_category() -> None:
    owned = SQLI.model_copy(update={"specialist": "correctness"})
    raw = (recorded("F1", "correctness", category="correctness"),)

    score = score_case(bug_case(owned), result(recorded=raw))

    assert score.specialist_caught == {"correctness": 1}


def test_a_finding_no_hook_attributed_is_scored_as_unattributed() -> None:
    score = score_case(bug_case(SQLI), result(recorded=(recorded("F1", None, line=40),)))

    assert score.specialist_false == {"unattributed": 1}


def test_a_bug_a_specialist_found_but_the_review_lacks_was_lost_by_the_lead() -> None:
    raw = (recorded("F1", "security"), recorded("F2", "security", line=40))
    kept_false = finding("security-1", line=40, status="unverified")

    score = score_case(bug_case(SQLI), result(kept_false, recorded=raw))

    assert score.lead_lost == 1
    assert (score.lead_kept_false, score.lead_removed_false) == (1, 0)


def test_keeping_a_duplicate_under_another_category_is_not_a_lead_loss() -> None:
    raw = (recorded("F1", "security"), recorded("F2", "conventions", category="conventions"))
    kept = finding("conventions-1", category="conventions", status="unverified")

    score = score_case(bug_case(SQLI), result(kept, recorded=raw))

    assert (score.lead_lost, score.lead_kept_false) == (0, 0)


def test_false_findings_the_lead_removed_are_counted() -> None:
    raw = (recorded("F1", "security", line=40), recorded("F2", "design", line=50))

    score = score_case(bug_case(SQLI), result(recorded=raw))

    assert (score.lead_kept_false, score.lead_removed_false) == (0, 2)


@pytest.mark.parametrize(
    ("severities", "held"),
    [
        (["nit"] * 5, True),
        (["nit"] * 6, False),
        (["high", "nit"], False),
        (["medium", "nit"], True),
    ],
)
def test_the_nit_cap_holds_at_five_and_at_none_beside_a_blocking_finding(
    severities: list[str], held: bool
) -> None:
    review = [finding(f"f{i}", line=40 + i, severity=s) for i, s in enumerate(severities)]

    assert score_case(bug_case(SQLI), result(*review)).nit_cap_held is held


def test_the_verifier_matrix_uses_its_own_answer_and_averages_confidence_per_cell() -> None:
    review = (
        finding("t-ok"),
        finding("t-low", line=15, status="rejected"),  # confirmed, but below the posting bar
        finding("t-no", line=16, status="rejected"),
        finding("f-no", line=40, status="rejected"),
        finding("f-ok", line=50),
    )
    answers = [
        verdict("t-ok", "confirmed", 0.9),
        verdict("t-low", "confirmed", 0.5),
        verdict("t-no", "rejected", 0.4),
        verdict("f-no", "rejected", 0.8),
        verdict("f-ok", "confirmed", 0.7),
    ]

    score = score_case(bug_case(SQLI), result(*review, verdicts=answers))
    summary = summarize([score])

    assert (score.true_bugs_confirmed, score.true_bugs_rejected) == (2, 1)
    assert (score.false_positives_confirmed, score.false_positives_rejected) == (1, 1)
    cells = summary.verifier_cells
    assert cells["confirmed_true"].count == 2
    assert cells["confirmed_true"].mean_confidence == pytest.approx(0.7)
    assert cells["rejected_true"].mean_confidence == pytest.approx(0.4)
    assert cells["rejected_false"].mean_confidence == pytest.approx(0.8)
    assert cells["confirmed_false"].mean_confidence == pytest.approx(0.7)
    assert summary.verifier_accuracy == pytest.approx(3 / 5)


SHELL_TRAP = Trap(file="app/tools.py", line_start=10, line_end=11, category="security")


def trap_case(name: str = "trap_shell") -> Case:
    return Case(
        name=name,
        path=Path("/cases") / name,
        expected=(),
        kind="verifier-trap",
        traps=(SHELL_TRAP,),
    )


def test_a_trap_is_triggered_by_a_specialist_and_posted_if_the_verifier_lets_it_by() -> None:
    flagged = recorded("F1", "security", file="app/tools.py", line=10)
    rejected = finding("security-1", file="app/tools.py", line=10, status="rejected")
    posted = finding("security-1", file="app/tools.py", line=10)

    caught = score_case(trap_case(), result(rejected, recorded=(flagged,)))
    missed = score_case(trap_case(), result(posted, recorded=(flagged,)))
    quiet = score_case(trap_case(), result())

    assert (caught.traps_triggered, caught.traps_posted) == (1, 0)
    assert (missed.traps_triggered, missed.traps_posted) == (1, 1)
    assert (quiet.traps_triggered, quiet.traps_posted) == (0, 0)
    assert caught.false_positives_rejected == 1
    summary = summarize([caught, missed, quiet])
    assert (summary.traps, summary.traps_triggered, summary.traps_posted) == (3, 2, 1)


def test_a_trap_case_is_not_a_clean_control() -> None:
    summary = summarize([score_case(trap_case(), result())])

    assert summary.clean_controls == 0


def test_specialist_scores_are_pooled_into_recall_and_precision() -> None:
    scores = [
        score_case(bug_case(SQLI), result(recorded=(recorded("F1", "security"),))),
        score_case(
            bug_case(SQLI, name="sqli_again"),
            result(recorded=(recorded("F1", "security", line=40),)),
        ),
    ]

    specialists = summarize(scores).specialists

    assert specialists["security"].recall == pytest.approx(1 / 2)
    assert specialists["security"].precision == pytest.approx(1 / 2)
    assert (specialists["security"].caught, specialists["security"].seeded) == (1, 2)


def test_a_failed_run_misses_every_bug_for_its_owner() -> None:
    assert failed_case(bug_case(SQLI), "boom", cost_usd=0.0).specialist_missed == {"security": 1}


def test_the_lead_summary_counts_losses_duplicates_and_cap_breaches() -> None:
    lost = score_case(bug_case(SQLI), result(recorded=(recorded("F1", "security"),)))
    nits = [finding(f"n{i}", line=60 + i, severity="nit") for i in range(6)]
    noisy = score_case(bug_case(SQLI, name="b"), result(finding("a"), finding("b", line=15), *nits))

    lead = summarize([lost, noisy]).lead

    assert (lead.lost, lead.duplicates, lead.nit_cap_breaches, lead.max_findings_breaches) == (
        1,
        1,
        1,
        0,
    )


def test_the_report_has_a_table_for_each_agent() -> None:
    scores = [
        score_case(bug_case(SQLI), result(finding("a"), recorded=(recorded("F1", "security"),))),
        score_case(trap_case(), result()),
    ]

    table = render_markdown({"verify": scores})

    assert "## Specialists" in table
    assert "| security | verify | 1 / 1 (100%) | 1 / 0 (100%) |" in table
    assert "## Lead" in table
    assert "## Verifier" in table
    assert "| verify | 1 (0.90) | 0 | 0 | 0 |" in table
    assert "## Verifier traps" in table
    assert "| verify | 1 | 0 | 0 |" in table
