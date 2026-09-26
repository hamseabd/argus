"""The eval runner's orchestration, driven offline by a fake agent. The live run is opt-in."""

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

import argus.agent.review
import evals.run
from argus.domain.errors import AgentRunError
from argus.domain.models import Finding, Review, ReviewContext, StageMetrics, Verdict
from argus.pipeline import StageOutcome
from evals.corpus import Case, load_cases
from evals.run import ResultPaths, result_label, run_eval

CASES = {case.name: case for case in load_cases()}
BUGS = {bug.file: bug for case in CASES.values() for bug in case.expected}


def metrics(stage: str, cost: float = 0.5) -> StageMetrics:
    return StageMetrics(
        stage=stage,
        model="m",
        cost_usd=cost,
        input_tokens=0,
        output_tokens=0,
        num_turns=3,
        duration_ms=10,
    )


class OracleAgent:
    """Reports exactly the seeded bug in whichever changed file carries one, and confirms it."""

    def __init__(self) -> None:
        self.roots: list[Path] = []
        self.verified: list[str] = []

    async def review(self, context: ReviewContext) -> StageOutcome[Review]:
        self.roots.append(context.repo_root)
        findings = [
            {
                "file": bug.file,
                "line": bug.line_start,
                "severity": bug.severity,
                "category": bug.category,
                "title": "seeded",
                "description": "d",
                "evidence": "e",
                "confidence": 0.9,
            }
            for f in context.files
            if (bug := BUGS.get(f.path))
        ]
        review = Review(summary="s", findings=findings, files_reviewed=[])
        return StageOutcome(review, metrics("review"), "sess")

    async def verify(self, context: ReviewContext, finding: Finding, diff_section: str):
        self.verified.append(finding.id)
        verdict = Verdict(finding_id=finding.id, verdict="confirmed", reasoning="r", confidence=1)
        return StageOutcome(verdict, metrics(f"verify:{finding.id}", 0.25), "sess")


class BrokenAgent(OracleAgent):
    async def review(self, context: ReviewContext) -> StageOutcome[Review]:
        raise AgentRunError("error_max_budget_usd", 3.0)


def run(agent, names, modes, out: Path):
    cases = [CASES[n] for n in names]
    return asyncio.run(run_eval(cases, agent, modes=modes, out_dir=out, label="2026-09-26-abc1234"))


def test_the_label_is_the_date_and_the_short_sha() -> None:
    assert result_label(date(2026, 9, 26), "abc1234") == "2026-09-26-abc1234"


def test_a_run_writes_the_json_record_and_the_markdown_table(tmp_path: Path) -> None:
    paths = run(OracleAgent(), ["sqli", "clean_docs_only"], ["verify", "no-verify"], tmp_path)

    assert paths.json == tmp_path / "2026-09-26-abc1234.json"
    assert paths.markdown == tmp_path / "2026-09-26-abc1234.md"
    record = json.loads(paths.json.read_text())
    assert list(record["modes"]) == ["verify", "no-verify"]
    for mode in ("verify", "no-verify"):
        summary = record["modes"][mode]["summary"]
        assert (summary["precision"], summary["recall"], summary["clean_control_fp_rate"]) == (
            1.0,
            1.0,
            0.0,
        )
        cases = record["modes"][mode]["cases"]
        assert [c["score"]["case"] for c in cases] == ["sqli", "clean_docs_only"]
        assert cases[0]["result"]["review"]["findings"][0]["file"] == "app/customers.py"
    assert record["modes"]["verify"]["summary"]["verifier_accuracy"] == 1.0
    assert record["modes"]["verify"]["summary"]["total_cost_usd"] == 1.25  # 2 reviews, 1 verify
    assert paths.markdown.read_text().startswith("| Mode |")


def test_no_verify_mode_never_calls_the_verifier(tmp_path: Path) -> None:
    agent = OracleAgent()

    run(agent, ["sqli"], ["no-verify"], tmp_path)

    assert agent.verified == []


def test_the_reviewer_cannot_see_the_case_name(tmp_path: Path) -> None:
    agent = OracleAgent()

    run(agent, ["path_traversal"], ["verify"], tmp_path)

    (root,) = agent.roots
    assert "path_traversal" not in str(root)
    assert "traversal" not in str(root)


def test_a_failed_review_is_scored_as_missed_with_its_cost_and_the_run_goes_on(
    tmp_path: Path,
) -> None:
    paths = run(BrokenAgent(), ["sqli", "off_by_one"], ["verify"], tmp_path)

    record = json.loads(paths.json.read_text())
    summary = record["modes"]["verify"]["summary"]
    assert (summary["cases"], summary["errors"], summary["false_negatives"]) == (2, 2, 2)
    assert summary["total_cost_usd"] == 6.0
    case = record["modes"]["verify"]["cases"][0]
    assert case["result"] is None
    assert "error_max_budget_usd" in case["score"]["error"]


def test_a_case_whose_repository_cannot_be_built_is_scored_as_failed_and_the_run_goes_on(
    tmp_path: Path,
) -> None:
    missing = Case(name="missing", path=tmp_path / "no-such-case", expected=CASES["sqli"].expected)
    cases = [missing, CASES["off_by_one"]]

    paths = asyncio.run(
        run_eval(cases, OracleAgent(), modes=["verify"], out_dir=tmp_path / "out", label="x")
    )

    record = json.loads(paths.json.read_text())
    broken, off_by_one = record["modes"]["verify"]["cases"]
    assert broken["result"] is None
    assert "no-such-case" in broken["score"]["error"]
    assert (broken["score"]["false_negatives"], broken["score"]["cost_usd"]) == (1, 0.0)
    assert off_by_one["score"]["error"] is None
    assert off_by_one["score"]["true_positives"] == 1


class FakeLiveRun:
    """Stands in for run_eval, so main() is driven end to end without a model call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, cases, agent, *, modes, out_dir, label, verify_concurrency):
        self.calls.append(
            {
                "cases": [c.name for c in cases],
                "modes": modes,
                "out_dir": out_dir,
                "verify_concurrency": verify_concurrency,
            }
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = ResultPaths(json=out_dir / f"{label}.json", markdown=out_dir / f"{label}.md")
        paths.markdown.write_text("| Mode | Precision |\n", encoding="utf-8")
        return paths


@pytest.fixture
def live_run(monkeypatch: pytest.MonkeyPatch) -> FakeLiveRun:
    fake = FakeLiveRun()
    monkeypatch.setattr(evals.run, "run_eval", fake)
    monkeypatch.setattr(evals.run, "_short_sha", lambda: "abc1234")
    monkeypatch.setattr(argus.agent.review, "SdkReviewAgent", lambda settings: object())
    return fake


def test_an_unknown_mode_is_a_bad_parameter(live_run: FakeLiveRun) -> None:
    result = CliRunner().invoke(evals.run.app, ["--mode", "fast"])

    assert result.exit_code == 2
    assert "Invalid value for --mode" in result.output
    assert "unknown mode 'fast'" in result.output
    assert live_run.calls == []


def test_an_unknown_case_is_a_bad_parameter(live_run: FakeLiveRun) -> None:
    result = CliRunner().invoke(evals.run.app, ["--case", "sqli", "--case", "nope"])

    assert result.exit_code == 2
    assert "Invalid value for --case" in result.output
    assert "no such case 'nope'" in result.output
    assert live_run.calls == []


def test_main_writes_under_out_and_echoes_the_table(
    live_run: FakeLiveRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARGUS_VERIFY_CONCURRENCY", "7")
    out = tmp_path / "record"

    result = CliRunner().invoke(
        evals.run.app, ["--mode", "verify", "--case", "sqli", "--out", str(out)]
    )

    assert result.exit_code == 0, result.output
    assert live_run.calls == [
        {"cases": ["sqli"], "modes": ["verify"], "out_dir": out, "verify_concurrency": 7}
    ]
    assert result.stdout == "| Mode | Precision |\n"


def test_main_defaults_to_every_mode_and_every_case(live_run: FakeLiveRun, tmp_path: Path) -> None:
    result = CliRunner().invoke(evals.run.app, ["--out", str(tmp_path)])

    assert result.exit_code == 0, result.output
    (call,) = live_run.calls
    assert call["modes"] == ["verify", "no-verify"]
    assert call["cases"] == [c.name for c in load_cases()]
