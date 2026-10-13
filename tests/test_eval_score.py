"""Scoring a review against a case's expected bugs, offline."""

from pathlib import Path

import pytest

from argus.domain.models import Finding, Review, ReviewResult, StageMetrics, Verdict
from evals.corpus import Case, ExpectedBug
from evals.score import (
    MAX_SPAN,
    CaseScore,
    failed_case,
    matches,
    render_markdown,
    score_case,
    summarize,
)

SQLI = ExpectedBug(
    file="app/customers.py", line_start=14, line_end=15, category="security", severity="high"
)


def bug_case(*expected: ExpectedBug, name: str = "sqli") -> Case:
    return Case(name=name, path=Path("/cases") / name, expected=expected)


def clean_case(name: str = "clean_docs_only") -> Case:
    return Case(name=name, path=Path("/cases") / name, expected=())


def finding(
    fid: str,
    *,
    file: str = "app/customers.py",
    line: int = 14,
    end_line: int | None = None,
    category: str = "security",
    status: str = "confirmed",
) -> Finding:
    return Finding(
        id=fid,
        file=file,
        line=line,
        end_line=end_line,
        severity="high",
        category=category,
        title="t",
        description="d",
        evidence="e",
        confidence=0.9,
        status=status,
    )


def result(*findings: Finding, cost: float = 1.0, turns=(5, 2), duration_ms: int = 9000):
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
        review=Review(summary="s", findings=list(findings), files_reviewed=[]),
        verdicts=[],
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
    assert lines[0].startswith("| Mode | Precision | Recall |")
    assert sum(line.startswith("| verify |") for line in lines) == 1
    assert sum(line.startswith("| no-verify |") for line in lines) == 1
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
            finding("b", file="app/other.py", category="quality"),
            finding("c", file="app/other.py", category="quality"),
            finding("d", file="app/other.py", category="correctness", status="rejected"),
        ),
    )

    assert score.true_positives_by_category == {"security": 1}
    assert score.false_positives_by_category == {"quality": 2}


def test_category_counts_are_pooled_into_the_summary_and_the_markdown() -> None:
    a = score_case(
        bug_case(SQLI), result(finding("a"), finding("b", file="x.py", category="quality"))
    )
    b = score_case(bug_case(SQLI), result(finding("c", file="y.py", category="quality")))

    summary = summarize([a, b])

    assert summary.by_category == {
        "security": {"true_positives": 1, "false_positives": 0, "precision": 1.0},
        "quality": {"true_positives": 0, "false_positives": 2, "precision": 0.0},
    }
    table = render_markdown({"verify": [a, b]})
    assert "| quality | 0 / 2 (0%) |" in table


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
        bug_case(SQLI),
        result(unsure).model_copy(
            update={
                "verdicts": [
                    Verdict(finding_id="a", verdict="inconclusive", reasoning="r", confidence=0.2)
                ]
            }
        ),
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
