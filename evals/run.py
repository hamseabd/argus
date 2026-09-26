"""Run Argus over the seeded-bug corpus and score it. Opt-in: it spends real quota.

    uv run python -m evals.run                   # every case, verify and no-verify
    uv run python -m evals.run --mode verify --case sqli

Each case is built into a fresh git repository in a temporary directory with
a neutral name, reviewed through the same pipeline and SdkReviewAgent the CLI
uses, and scored by evals.score. Cases run one at a time, so latency is a
single review's and the quota window is not flooded. The record lands in
evals/results/<date>-<sha>.json, every ReviewResult included, with the table
beside it as .md.

Needs CLAUDE_CODE_OAUTH_TOKEN, or the machine's Claude login. The SDK is
imported lazily inside main(), so the orchestration below is tested offline
with a fake agent and importing this module never loads the SDK.
"""

import asyncio
import json
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from argus import telemetry
from argus.context.git import local_context
from argus.domain.errors import ArgusError
from argus.domain.models import ReviewResult
from argus.pipeline import DEFAULT_VERIFY_CONCURRENCY, PAID_ERRORS, ReviewAgent, run_review
from argus.settings import Settings
from evals.corpus import Case, build_case_repo, load_cases
from evals.score import CaseScore, failed_case, render_markdown, score_case, summarize

ROOT = Path(__file__).parent.parent
RESULTS_DIR = Path(__file__).parent / "results"
MODES = {"verify": True, "no-verify": False}


@dataclass(frozen=True)
class ResultPaths:
    json: Path
    markdown: Path


def result_label(day: date, sha: str) -> str:
    return f"{day.isoformat()}-{sha}"


async def run_case(
    case: Case, agent: ReviewAgent, *, verify: bool, verify_concurrency: int
) -> tuple[CaseScore, ReviewResult | None]:
    """Build the case's repository, review it, and score the result.

    A run that fails is scored as having missed every seeded bug, with what it
    cost, so one bad case never loses the rest of the corpus.
    """
    log = telemetry.get_logger()
    with tempfile.TemporaryDirectory() as tmp:
        # A neutral directory name: the reviewer sees the repository root, and
        # a name like "sqli" would tell it what to look for.
        repo = build_case_repo(case, Path(tmp) / "repo")
        context = local_context(repo, base="main")
        try:
            result = await run_review(
                context, agent, verify=verify, verify_concurrency=verify_concurrency
            )
        except ArgusError as exc:
            cost = float(exc.cost_usd) if isinstance(exc, PAID_ERRORS) else 0.0
            log.warning("eval_case_failed", case=case.name, error=str(exc), cost_usd=cost)
            return failed_case(case, str(exc), cost), None
    score = score_case(case, result)
    log.info("eval_case_scored", verify=verify, **score.model_dump())
    return score, result


async def run_eval(
    cases: Sequence[Case],
    agent: ReviewAgent,
    *,
    modes: Sequence[str],
    out_dir: Path,
    label: str,
    verify_concurrency: int = DEFAULT_VERIFY_CONCURRENCY,
) -> ResultPaths:
    """Every case in every mode, one at a time; writes <label>.json and <label>.md."""
    record: dict[str, Any] = {"label": label, "modes": {}}
    scores_by_mode: dict[str, list[CaseScore]] = {}
    for mode in modes:
        entries = []
        for case in cases:
            score, result = await run_case(
                case, agent, verify=MODES[mode], verify_concurrency=verify_concurrency
            )
            entries.append(
                {
                    "score": score.model_dump(mode="json"),
                    "result": None if result is None else result.model_dump(mode="json"),
                }
            )
            scores_by_mode.setdefault(mode, []).append(score)
        summary = summarize(scores_by_mode.get(mode, []))
        record["modes"][mode] = {"summary": summary.model_dump(mode="json"), "cases": entries}
    markdown = render_markdown(scores_by_mode)
    record["markdown"] = markdown
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = ResultPaths(json=out_dir / f"{label}.json", markdown=out_dir / f"{label}.md")
    paths.json.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    paths.markdown.write_text(markdown, encoding="utf-8")
    telemetry.get_logger().info("eval_written", json=str(paths.json), markdown=str(paths.markdown))
    return paths


def _short_sha() -> str:
    """HEAD of the Argus checkout under evaluation, marked -dirty with uncommitted changes."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout.strip()

    sha = git("rev-parse", "--short", "HEAD")
    return f"{sha}-dirty" if git("status", "--porcelain", "--untracked-files=no") else sha


app = typer.Typer(add_completion=False)


@app.command()
def main(
    mode: Annotated[
        list[str] | None,
        typer.Option("--mode", help="verify or no-verify; repeatable. Default: both."),
    ] = None,
    case: Annotated[
        list[str] | None, typer.Option("--case", help="Run only this case; repeatable.")
    ] = None,
    out: Annotated[Path, typer.Option("--out", help="Directory for the record.")] = RESULTS_DIR,
) -> None:
    """Review every corpus case with the real SDK and write the scored record."""
    from argus.agent.review import SdkReviewAgent  # the SDK loads only for a live run

    modes = mode or list(MODES)
    if unknown := [m for m in modes if m not in MODES]:
        raise typer.BadParameter(f"unknown mode {unknown[0]!r}", param_hint="--mode")
    cases = load_cases()
    if case:
        if missing := sorted(set(case) - {c.name for c in cases}):
            raise typer.BadParameter(f"no such case {missing[0]!r}", param_hint="--case")
        cases = [c for c in cases if c.name in case]
    settings = Settings()
    telemetry.configure(settings.log_format, level=settings.log_level)
    label = result_label(datetime.now(UTC).date(), _short_sha())
    paths = asyncio.run(
        run_eval(
            cases,
            SdkReviewAgent(settings),
            modes=modes,
            out_dir=out,
            label=label,
            verify_concurrency=settings.verify_concurrency,
        )
    )
    typer.echo(paths.markdown.read_text(encoding="utf-8"), nl=False)


if __name__ == "__main__":
    app()
