"""The eval gate: thresholds, the stored baseline, and the comparison a pull request must pass."""

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import evals.gate
from evals.corpus import load_cases
from evals.gate import (
    THRESHOLDS_PATH,
    Baseline,
    BaselineCase,
    Thresholds,
    build_baseline,
    check,
    load_thresholds,
    render_check,
    rescore,
)
from evals.run import run_eval
from evals.score import CaseScore
from tests.test_eval_run import OracleAgent

CASES = {case.name: case for case in load_cases()}


def baseline(**cases: BaselineCase) -> Baseline:
    return Baseline.model_validate(
        {
            "label": "2026-11-02-abc1234",
            "commit": "abc1234",
            "config": None,
            "summary": None,
            "cases": cases,
        }
    )


def caught() -> BaselineCase:
    return BaselineCase(true_positives=1, false_negatives=0, reported=1)


def score(name: str, *, tp: int = 1, fn: int = 0, fp: int = 0, error: str | None = None):
    return CaseScore(
        case=name,
        clean=name.startswith("clean_"),
        true_positives=tp,
        false_negatives=fn,
        false_positives=fp,
        error=error,
    )


THRESHOLDS = Thresholds(smoke=["a", "b"], max_extra_misses=0, max_clean_control_gain=0)


# --- thresholds -------------------------------------------------------------


def test_the_committed_thresholds_load_and_name_only_real_cases() -> None:
    thresholds = load_thresholds()

    assert set(thresholds.smoke) <= set(CASES)
    kinds = [CASES[name].kind for name in thresholds.smoke]
    assert kinds.count("clean") == 2
    assert kinds.count("verifier-trap") == 1
    owners = {bug.owner for name in thresholds.smoke for bug in CASES[name].expected}
    assert owners == {
        "correctness",
        "security",
        "tests",
        "maintainability",
        "design",
        "conventions",
    }


def test_thresholds_with_an_unknown_key_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "thresholds.yaml"
    path.write_text(yaml.safe_dump({**load_thresholds().model_dump(), "max_drop": 0.1}))

    with pytest.raises(ValueError, match="max_drop"):
        load_thresholds(path)


def test_the_thresholds_live_beside_the_corpus() -> None:
    assert Path(evals.gate.__file__).parent / "thresholds.yaml" == THRESHOLDS_PATH


# --- comparison -------------------------------------------------------------


def test_a_run_that_matches_the_baseline_passes() -> None:
    outcome = check([score("a"), score("clean_b", tp=0)], baseline(a=caught()), THRESHOLDS)

    assert outcome.passed
    assert outcome.regressions == []


def test_a_bug_the_baseline_caught_and_this_run_missed_fails_the_gate() -> None:
    outcome = check([score("a", tp=0, fn=1)], baseline(a=caught()), THRESHOLDS)

    assert not outcome.passed
    assert outcome.regressions == ["recall: 1 seeded bug missed that the baseline caught (a)"]


def test_missed_bugs_within_the_tolerance_pass() -> None:
    thresholds = THRESHOLDS.model_copy(update={"max_extra_misses": 1})

    outcome = check([score("a", tp=0, fn=1)], baseline(a=caught()), thresholds)

    assert outcome.passed


def test_recall_is_compared_over_the_same_cases_only() -> None:
    held = baseline(a=caught(), z=BaselineCase(true_positives=0, false_negatives=1, reported=0))

    outcome = check([score("a")], held, THRESHOLDS)

    assert (outcome.baseline_recall, outcome.recall) == (1.0, 1.0)


def test_a_clean_control_that_gains_a_finding_fails_the_gate() -> None:
    held = baseline(clean_b=BaselineCase(true_positives=0, false_negatives=0, reported=0))

    outcome = check([score("clean_b", tp=0, fp=1)], held, THRESHOLDS)

    assert outcome.regressions == ["clean_b: 1 finding on a clean control, baseline 0"]


def test_a_case_that_errored_fails_the_gate_because_nothing_was_measured() -> None:
    outcome = check([score("a", tp=0, fn=1, error="boom")], baseline(a=caught()), THRESHOLDS)

    assert "a: errored, so it was not measured" in outcome.regressions


def test_a_case_the_baseline_lacks_is_reported_but_not_compared() -> None:
    outcome = check([score("new", tp=0, fn=1)], baseline(), THRESHOLDS)

    assert outcome.passed
    assert outcome.unbaselined == ["new"]


def test_the_check_renders_a_verdict_and_both_recalls() -> None:
    outcome = check([score("a", tp=0, fn=1)], baseline(a=caught()), THRESHOLDS)

    text = render_check(outcome, baseline(a=caught()))

    assert text.startswith("## Argus eval: FAIL")
    assert "abc1234" in text
    assert "| Recall on these cases | 100% | 0% |" in text
    assert "- recall: 1 seeded bug missed that the baseline caught (a)" in text


# --- baseline from a record -------------------------------------------------


def record_of(tmp_path: Path, names: list[str], trials: int = 1) -> Path:
    paths = asyncio.run(
        run_eval(
            [CASES[n] for n in names],
            OracleAgent(),
            modes=["verify"],
            out_dir=tmp_path,
            label="2026-11-02-abc1234",
            trials=trials,
        )
    )
    return paths.json


def test_a_baseline_is_rescored_from_the_stored_results(tmp_path: Path) -> None:
    record = json.loads(record_of(tmp_path, ["sqli", "clean_docs_only"]).read_text())

    held = build_baseline(record, CASES)

    assert held.commit == "abc1234"
    assert held.cases["sqli"] == BaselineCase(true_positives=1, false_negatives=0, reported=1)
    assert held.cases["clean_docs_only"].reported == 0
    assert held.summary is not None
    assert held.summary.recall == 1.0


def test_a_baseline_needs_a_single_trial_verify_run(tmp_path: Path) -> None:
    record = json.loads(record_of(tmp_path, ["sqli"], trials=2).read_text())

    with pytest.raises(ValueError, match="one trial"):
        build_baseline(record, CASES)


# --- command line -----------------------------------------------------------


def test_check_exits_one_on_a_regression_and_prints_the_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = record_of(tmp_path, ["sqli", "off_by_one"])
    held = baseline(sqli=caught(), off_by_one=caught())
    record = json.loads(path.read_text())
    for entry in record["modes"]["verify"]["cases"]:  # two misses: past any tolerance
        entry["result"]["review"]["findings"] = []
    path.write_text(json.dumps(record))
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(held.model_dump_json())

    result = CliRunner().invoke(
        evals.gate.app, ["check", str(path), "--baseline", str(baseline_path)]
    )

    assert result.exit_code == 1
    assert "## Argus eval: FAIL" in result.stdout


def test_check_without_a_baseline_passes_and_says_so(tmp_path: Path) -> None:
    path = record_of(tmp_path, ["sqli"])

    result = CliRunner().invoke(
        evals.gate.app, ["check", str(path), "--baseline", str(tmp_path / "none.json")]
    )

    assert result.exit_code == 0
    assert "no baseline" in result.stdout


def test_check_without_a_baseline_still_fails_when_a_case_errored(tmp_path: Path) -> None:
    path = record_of(tmp_path, ["sqli"])
    record = json.loads(path.read_text())
    entry = record["modes"]["verify"]["cases"][0]
    entry["result"], entry["score"]["error"] = None, "api status 429"
    path.write_text(json.dumps(record))

    result = CliRunner().invoke(
        evals.gate.app, ["check", str(path), "--baseline", str(tmp_path / "none.json")]
    )

    assert result.exit_code == 1
    assert "## Argus eval: FAIL, no baseline" in result.stdout
    assert "- sqli: errored, so it was not measured" in result.stdout


def test_baseline_writes_the_baseline_and_the_report(tmp_path: Path) -> None:
    path = record_of(tmp_path, ["sqli"])
    out = tmp_path / "baseline.json"
    report = tmp_path / "report.md"

    result = CliRunner().invoke(
        evals.gate.app,
        ["baseline", str(path), "--out", str(out), "--report", str(report)],
    )

    assert result.exit_code == 0, result.output
    assert Baseline.model_validate_json(out.read_text()).cases["sqli"].true_positives == 1
    assert "## Specialists" in report.read_text()


def missed() -> BaselineCase:
    return BaselineCase(true_positives=0, false_negatives=1, reported=0)


def test_a_bug_the_baseline_missed_too_is_no_regression() -> None:
    outcome = check([score("a", tp=0, fn=1)], baseline(a=missed()), THRESHOLDS)

    assert outcome.passed


def test_a_miss_the_baseline_had_on_another_case_excuses_nothing() -> None:
    held = baseline(a=caught(), z=missed())

    outcome = check([score("a", tp=0, fn=1)], held, THRESHOLDS)

    assert outcome.regressions == ["recall: 1 seeded bug missed that the baseline caught (a)"]


def test_a_catch_on_one_case_never_offsets_a_new_miss_on_another() -> None:
    held = baseline(a=caught(), b=missed())

    outcome = check([score("a", tp=0, fn=1), score("b")], held, THRESHOLDS)

    assert outcome.regressions == ["recall: 1 seeded bug missed that the baseline caught (a)"]


def test_an_errored_case_counts_once_as_an_error_not_again_as_a_miss() -> None:
    outcome = check([score("a", tp=0, fn=1, error="boom")], baseline(a=caught()), THRESHOLDS)

    assert outcome.regressions == ["a: errored, so it was not measured"]


def test_with_no_baseline_only_errors_fail() -> None:
    clean = check([score("a", tp=0, fn=1)], None, THRESHOLDS)
    errored = check([score("a", error="boom")], None, THRESHOLDS)

    assert clean.passed
    assert errored.regressions == ["a: errored, so it was not measured"]
    assert render_check(errored, None).startswith("## Argus eval: FAIL, no baseline")


def test_check_reads_the_thresholds_it_is_given(tmp_path: Path) -> None:
    path = record_of(tmp_path, ["sqli"])
    record = json.loads(path.read_text())
    record["modes"]["verify"]["cases"][0]["result"]["review"]["findings"] = []
    path.write_text(json.dumps(record))
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(baseline(sqli=caught()).model_dump_json())
    strict = tmp_path / "thresholds.yaml"
    strict.write_text(yaml.safe_dump({**load_thresholds().model_dump(), "max_extra_misses": 0}))

    result = CliRunner().invoke(
        evals.gate.app,
        ["check", str(path), "--baseline", str(baseline_path), "--thresholds", str(strict)],
    )

    assert result.exit_code == 1


# --- cases that vanish, and records the default branch cannot read --------------


def test_a_baseline_case_the_run_should_have_covered_but_skipped_fails_the_gate() -> None:
    held = baseline(a=caught(), b=caught())

    outcome = check(
        [score("a")], held, THRESHOLDS, expected=["a", "b"], ran=["a"], corpus=["a", "b"]
    )

    assert outcome.regressions == ["b: in the baseline but not in this run"]


def test_a_baseline_case_the_change_removed_from_the_corpus_is_listed_not_failed() -> None:
    held = baseline(a=caught(), b=caught())

    outcome = check([score("a")], held, THRESHOLDS, expected=["a", "b"], ran=["a"], corpus=["a"])

    assert outcome.passed
    assert outcome.removed == ["b"]
    assert "Removed from the corpus by this change: `b`." in render_check(outcome, held)


def test_without_a_corpus_a_missing_case_is_a_regression() -> None:
    outcome = check([], baseline(b=caught()), THRESHOLDS, expected=["b"], ran=[], corpus=None)

    assert outcome.regressions == ["b: in the baseline but not in this run"]


def test_a_case_the_run_was_not_meant_to_cover_is_not_missing() -> None:
    held = baseline(a=caught(), b=caught())

    outcome = check([score("a")], held, THRESHOLDS, expected=["a"], ran=["a"], corpus=["a", "b"])

    assert outcome.passed


def test_a_case_the_gates_corpus_lacks_is_listed_and_left_unscored(tmp_path: Path) -> None:
    record = json.loads(record_of(tmp_path, ["sqli", "off_by_one"]).read_text())

    rescored = rescore(record, {"sqli": CASES["sqli"]}, "verify")

    assert [s.case for s in rescored.scores] == ["sqli"]
    assert rescored.unknown == ["off_by_one"]


def test_a_result_the_gates_models_cannot_read_is_an_error_not_its_stored_score(
    tmp_path: Path,
) -> None:
    record = json.loads(record_of(tmp_path, ["sqli"]).read_text())
    record["modes"]["verify"]["cases"][0]["result"]["review"]["added_later"] = []

    (scored,) = rescore(record, CASES, "verify").scores

    assert scored.error is not None and "cannot read this result" in scored.error
    assert scored.true_positives == 0  # the run's own claim is never taken


def test_a_baseline_is_never_built_from_cases_or_results_it_cannot_score(tmp_path: Path) -> None:
    record = json.loads(record_of(tmp_path, ["sqli", "off_by_one"]).read_text())

    with pytest.raises(ValueError, match="off_by_one"):
        build_baseline(record, {"sqli": CASES["sqli"]})


def test_check_with_smoke_expects_every_smoke_case_in_the_baseline(tmp_path: Path) -> None:
    path = record_of(tmp_path, ["sqli"])
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(baseline(sqli=caught(), none_deref=caught()).model_dump_json())
    smoke = tmp_path / "thresholds.yaml"
    smoke.write_text(
        yaml.safe_dump({**load_thresholds().model_dump(), "smoke": ["sqli", "none_deref"]})
    )

    result = CliRunner().invoke(
        evals.gate.app,
        [
            "check",
            str(path),
            "--baseline",
            str(baseline_path),
            "--thresholds",
            str(smoke),
            "--smoke",
        ],
    )

    assert result.exit_code == 1
    assert "none_deref: in the baseline but not in this run" in result.stdout


def test_a_full_run_reads_the_changes_corpus_from_the_directory_it_is_given(
    tmp_path: Path,
) -> None:
    path = record_of(tmp_path, ["sqli"])
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(baseline(sqli=caught(), gone=caught()).model_dump_json())
    corpus = tmp_path / "head-cases"
    (corpus / "sqli" / "base").mkdir(parents=True)
    (corpus / "sqli" / "seeded").mkdir()
    (corpus / "sqli" / "expected.yaml").write_text(
        (CASES["sqli"].path / "expected.yaml").read_text()
    )

    result = CliRunner().invoke(
        evals.gate.app,
        ["check", str(path), "--baseline", str(baseline_path), "--corpus", str(corpus)],
    )

    assert result.exit_code == 0, result.stdout
    assert "Removed from the corpus by this change: `gone`." in result.stdout


def test_a_baseline_is_never_built_from_an_errored_run(tmp_path: Path) -> None:
    record = json.loads(record_of(tmp_path, ["sqli"]).read_text())
    entry = record["modes"]["verify"]["cases"][0]
    entry["result"], entry["score"]["error"] = None, "api status 429"

    with pytest.raises(ValueError, match="errored"):
        build_baseline(record, CASES)
