"""The eval gate: a pull request's eval run against the baseline from main.

    uv run python -m evals.gate check evals/results/<label>.json     # exit 1 on a regression
    uv run python -m evals.gate baseline evals/results/<label>.json  # baseline.json, report.md

evals/baseline.json holds the last full verify-mode run on main: its summary,
every agent's numbers, and each case's counts, with the commit and the
models. A pull request's run, the smoke subset or the full corpus, is held
to it case for case, so a subset is only ever compared with the same subset:

- recall: case by case, the seeded bugs this run missed beyond the ones the
  baseline missed on that case; more than max_extra_misses of them in all
  fail the gate. A catch on one case never offsets a new miss on another;
- clean controls: a control reporting more findings than it did in the
  baseline, by more than max_clean_control_gain, fails the gate;
- an errored case fails the gate: it was not measured, so it cannot pass;
- a baseline case the run should have covered (the smoke subset, or every
  case for a full run) but did not fails the gate, unless the change removed
  it from the corpus, which the gate lists instead.

A case the baseline lacks (a new one) is listed but not compared; with no
baseline at all only the errored-case rule applies. The thresholds live in
evals/thresholds.yaml, never in the workflow. On a pull request this module,
the scorer, the corpus, the baseline, and the thresholds all run from the
default branch, so a change cannot loosen its own gate; only the reviewer
under test comes from the change. A result the default branch's models cannot
read (the change altered them) counts as an error: the gate fails closed, and
a maintainer merges past it knowingly. The record itself is written by the
change's own code, so the gate trusts it to have reviewed what it names; it
guards against a change loosening what it is judged by, not against a
dishonest one, which review catches. A baseline
is rescored from the stored results, so a scorer change never needs a second
paid run to refresh it.
"""

import json
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from argus.domain.models import ReviewResult, RunConfig
from evals.corpus import Case, load_cases
from evals.report import pct, render_markdown
from evals.score import CaseScore, Summary, failed_case, ratio, score_case, summarize

EVALS_DIR = Path(__file__).parent
THRESHOLDS_PATH = EVALS_DIR / "thresholds.yaml"
BASELINE_PATH = EVALS_DIR / "baseline.json"
REPORT_PATH = EVALS_DIR / "report.md"
BASELINE_MODE = "verify"
"""The mode the baseline and the gate run in: the one a posted review uses."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Thresholds(_Model):
    smoke: list[str] = Field(min_length=1, description="The cases a pull request runs.")
    max_extra_misses: int = Field(ge=0)
    max_clean_control_gain: int = Field(ge=0)


class BaselineCase(_Model):
    true_positives: int
    false_negatives: int
    reported: int


class Baseline(_Model):
    label: str
    commit: str
    config: RunConfig | None
    summary: Summary | None
    cases: dict[str, BaselineCase]


class Rescored(_Model):
    """A stored run scored again by this checkout's scorer and corpus."""

    scores: list[CaseScore]
    unknown: list[str] = Field(default_factory=list, description="Cases this corpus lacks.")


class CheckOutcome(_Model):
    regressions: list[str]
    unbaselined: list[str]
    removed: list[str] = Field(default_factory=list)
    unscored: list[str] = Field(default_factory=list)
    recall: float | None
    baseline_recall: float | None
    missed: int
    baseline_missed: int
    clean_control_findings: int
    baseline_clean_control_findings: int

    @property
    def passed(self) -> bool:
        return not self.regressions


def load_thresholds(path: Path = THRESHOLDS_PATH) -> Thresholds:
    try:
        return Thresholds.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except ValueError as exc:
        raise ValueError(f"{path.name}: {exc}") from exc


def check(
    scores: Sequence[CaseScore],
    baseline: Baseline | None,
    thresholds: Thresholds,
    *,
    expected: Collection[str] = (),
    ran: Collection[str] | None = None,
    corpus: Collection[str] | None = None,
) -> CheckOutcome:
    """This run's scores, case for case, against the baseline's, or alone when there is none.

    expected names the baseline cases the run should have covered; ran, every
    case it did (those scored and those this gate could not); corpus, every
    case the change's corpus holds, read by the gate from the change's commit,
    never from the record, so a case the change removed is told from one the
    run skipped. Without a corpus (a smoke run, which reviews the default
    branch's cases), every missing case is a regression.
    """
    regressions = [f"{s.case}: errored, so it was not measured" for s in scores if s.error]
    held_cases = baseline.cases if baseline is not None else {}
    covered = set(ran) if ran is not None else {s.case for s in scores}
    removed = []
    for name in sorted(set(expected) & set(held_cases) - covered):
        if corpus is not None and name not in corpus:
            removed.append(name)
        else:
            regressions.append(f"{name}: in the baseline but not in this run")
    compared = [s for s in scores if s.case in held_cases]
    held = [held_cases[s.case] for s in compared]
    caught = sum(s.true_positives for s in compared)
    missed = sum(s.false_negatives for s in compared)
    baseline_caught = sum(b.true_positives for b in held)
    baseline_missed = sum(b.false_negatives for b in held)
    new_misses = {
        s.case: s.false_negatives - b.false_negatives
        for s, b in zip(compared, held, strict=True)
        if not s.error and s.false_negatives > b.false_negatives
    }
    if (extra := sum(new_misses.values())) > thresholds.max_extra_misses:
        noun = "bug" if extra == 1 else "bugs"
        regressions.append(
            f"recall: {extra} seeded {noun} missed that the baseline caught "
            f"({', '.join(sorted(new_misses))})"
        )
    controls = [(s, b) for s, b in zip(compared, held, strict=True) if s.clean and not s.error]
    for s, b in controls:
        if s.reported - b.reported > thresholds.max_clean_control_gain:
            noun = "finding" if s.reported == 1 else "findings"
            regressions.append(
                f"{s.case}: {s.reported} {noun} on a clean control, baseline {b.reported}"
            )
    return CheckOutcome(
        regressions=regressions,
        unbaselined=sorted({s.case for s in scores} - set(held_cases)),
        removed=removed,
        recall=ratio(caught, caught + missed),
        baseline_recall=ratio(baseline_caught, baseline_caught + baseline_missed),
        missed=missed,
        baseline_missed=baseline_missed,
        clean_control_findings=sum(s.reported for s, _ in controls),
        baseline_clean_control_findings=sum(b.reported for _, b in controls),
    )


def render_check(outcome: CheckOutcome, baseline: Baseline | None) -> str:
    verdict = "PASS" if outcome.passed else "FAIL"
    if baseline is None:
        lines = [f"## Argus eval: {verdict}, no baseline", "", "Nothing to compare with yet."]
    else:
        lines = [
            f"## Argus eval: {verdict}",
            "",
            f"Against the baseline from `{baseline.commit}` ({baseline.label}), case for case.",
            "",
            "| | Baseline | This run |",
            "|---|---|---|",
            f"| Recall on these cases | {pct(outcome.baseline_recall)} | {pct(outcome.recall)} |",
            f"| Seeded bugs missed | {outcome.baseline_missed} | {outcome.missed} |",
            f"| Findings on clean controls | {outcome.baseline_clean_control_findings} "
            f"| {outcome.clean_control_findings} |",
        ]
    if outcome.regressions:
        lines += ["", "Regressions:", *(f"- {r}" for r in outcome.regressions)]
    for names, note in (
        (
            outcome.unbaselined if baseline is not None else [],
            "Not in the baseline, so not compared",
        ),
        (outcome.removed, "Removed from the corpus by this change"),
        (outcome.unscored, "Not in the default branch's corpus, so not scored"),
    ):
        if names:
            lines += ["", f"{note}: {', '.join(f'`{n}`' for n in names)}."]
    return "\n".join(lines) + "\n"


def rescore(record: Mapping[str, Any], cases: Mapping[str, Case], mode: str) -> Rescored:
    """Every case of one mode in a stored record, scored again with this scorer and corpus.

    A case this corpus lacks is left out and named. A result these models
    cannot read counts as an error: nothing the run stored is scored in its place.
    """
    scores, unknown = [], []
    for entry in record["modes"][mode]["cases"]:
        raw = entry["score"]
        name, trial = str(raw["case"]), int(raw.get("trial", 1))
        case = cases.get(name)
        if case is None:
            unknown.append(name)
            continue
        if entry["result"] is None:
            fresh = failed_case(
                case, str(raw.get("error") or "no result"), raw.get("cost_usd", 0.0)
            )
        else:
            try:
                fresh = score_case(case, ReviewResult.model_validate(entry["result"]))
            except ValidationError as exc:
                reason = f"the default branch's models cannot read this result: {exc}"
                fresh = failed_case(case, reason.splitlines()[0], float(raw.get("cost_usd", 0.0)))
        scores.append(fresh.model_copy(update={"trial": trial}))
    return Rescored(scores=scores, unknown=unknown)


def build_baseline(record: Mapping[str, Any], cases: Mapping[str, Case]) -> Baseline:
    if record.get("trials", 1) != 1:
        raise ValueError("a baseline comes from one trial; rerun with --trials 1")
    if BASELINE_MODE not in record["modes"]:
        raise ValueError(f"a baseline comes from a {BASELINE_MODE} run")
    rescored = rescore(record, cases, BASELINE_MODE)
    if rescored.unknown:
        raise ValueError(f"cannot score {', '.join(rescored.unknown)} with this corpus")
    if errors := [s.case for s in rescored.scores if s.error]:
        raise ValueError(f"cannot baseline a run with errored cases: {', '.join(errors)}")
    scores = rescored.scores
    label = str(record["label"])
    return Baseline(
        label=label,
        commit=label.split("-", 3)[-1],  # the label is <yyyy-mm-dd>-<sha>
        config=record.get("config"),
        summary=summarize(scores),
        cases={
            s.case: BaselineCase(
                true_positives=s.true_positives,
                false_negatives=s.false_negatives,
                reported=s.reported,
            )
            for s in scores
        },
    )


def write_report(
    path: Path,
    scores_by_mode: Mapping[str, list[CaseScore]],
    label: str,
    config: RunConfig | None,
) -> None:
    """evals/report.md: which run it is, what it ran, then every table."""
    lines = ["# Argus eval report", "", f"Run `{label}`."]
    if config is not None:
        lines.append(
            f"Lead `{config.lead_model}` ({config.lead_effort}), specialists "
            f"`{config.specialist_model}` ({config.specialist_effort}), verifier "
            f"`{config.verifier_model}` ({config.verifier_effort}); prompts `{config.prompts_sha}`."
        )
    lines += ["", "How to read it and how it is produced: see Evals in the README.", ""]
    path.write_text("\n".join(lines) + "\n" + render_markdown(scores_by_mode), encoding="utf-8")


app = typer.Typer(add_completion=False, no_args_is_help=True)


def _load_record(path: Path) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return record


@app.command("check")
def check_command(
    record_path: Annotated[Path, typer.Argument(help="The run's JSON record.")],
    baseline_path: Annotated[Path, typer.Option("--baseline")] = BASELINE_PATH,
    thresholds_path: Annotated[Path, typer.Option("--thresholds")] = THRESHOLDS_PATH,
    smoke: Annotated[
        bool, typer.Option("--smoke", help="The run was the smoke subset, not every case.")
    ] = False,
    corpus_dir: Annotated[
        Path | None,
        typer.Option(
            "--corpus",
            help="The change's own corpus, checked out fresh, for a full run's removed cases.",
        ),
    ] = None,
) -> None:
    """Print the run against the baseline; exit 1 on a regression.

    On a pull request this runs from the default branch's checkout, with its
    corpus, baseline, and thresholds, so the change is gated by what it cannot
    change.
    """
    record = _load_record(record_path)
    rescored = rescore(record, {c.name: c for c in load_cases()}, BASELINE_MODE)
    baseline = (
        Baseline.model_validate_json(baseline_path.read_text(encoding="utf-8"))
        if baseline_path.exists()
        else None
    )
    thresholds = load_thresholds(thresholds_path)
    expected = thresholds.smoke if smoke else list(baseline.cases if baseline else [])
    outcome = check(
        rescored.scores,
        baseline,
        thresholds,
        expected=expected,
        ran=[str(e["score"]["case"]) for e in record["modes"][BASELINE_MODE]["cases"]],
        corpus=None if corpus_dir is None else [c.name for c in load_cases(corpus_dir)],
    ).model_copy(update={"unscored": rescored.unknown})
    typer.echo(render_check(outcome, baseline))
    typer.echo(render_markdown({BASELINE_MODE: rescored.scores}), nl=False)
    if not outcome.passed:
        raise typer.Exit(1)


@app.command("baseline")
def baseline_command(
    record_path: Annotated[Path, typer.Argument(help="A full single-trial verify run.")],
    out: Annotated[Path, typer.Option("--out")] = BASELINE_PATH,
    report: Annotated[Path, typer.Option("--report")] = REPORT_PATH,
) -> None:
    """Write the baseline and the report from a stored run, rescored."""
    record = _load_record(record_path)
    cases = {c.name: c for c in load_cases()}
    baseline = build_baseline(record, cases)
    out.write_text(baseline.model_dump_json(indent=2) + "\n", encoding="utf-8")
    write_report(
        report,
        {BASELINE_MODE: rescore(record, cases, BASELINE_MODE).scores},
        baseline.label,
        baseline.config,
    )
    typer.echo(f"wrote {out} and {report}")


if __name__ == "__main__":
    app()
