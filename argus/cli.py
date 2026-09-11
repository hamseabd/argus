"""Argus command line."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from argus import __version__, telemetry
from argus.auth import credential_problem, credential_source
from argus.context.git import local_context
from argus.domain.errors import ArgusError
from argus.domain.models import SEVERITY_ORDER, Finding, ReviewResult
from argus.pipeline import run_review
from argus.report.markdown import render_report
from argus.settings import Settings

EXIT_ERROR = 1
EXIT_GATE = 2

app = typer.Typer(
    help="Argus: a code-review agent on the Claude Agent SDK.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Argus command line."""


@app.command()
def version() -> None:
    """Print the Argus version."""
    typer.echo(f"argus {__version__}")


@app.command()
def review(
    diff: Annotated[
        bool, typer.Option("--diff", help="Review the local diff against --base.")
    ] = False,
    base: Annotated[str, typer.Option("--base", help="Base ref for --diff.")] = "main",
    json_path: Annotated[
        Path | None, typer.Option("--json", help="Write the ReviewResult here.")
    ] = None,
    no_verify: Annotated[bool, typer.Option("--no-verify", help="Skip the verify stage.")] = False,
    fail_on: Annotated[
        str | None,
        typer.Option(
            "--fail-on", help="Exit 2 if any reported finding is at or above this severity."
        ),
    ] = None,
) -> None:
    """Review a change and print the findings."""
    if not diff:
        raise typer.BadParameter("choose a mode: --diff")
    if fail_on is not None and fail_on not in SEVERITY_ORDER:
        raise typer.BadParameter(
            f"{fail_on!r} is not a severity; expected one of {', '.join(SEVERITY_ORDER)}",
            param_hint="--fail-on",
        )
    settings = Settings()
    telemetry.configure(settings.log_format, level=settings.log_level)
    _check_credentials()

    from argus.agent.review import SdkReviewAgent

    try:
        context = local_context(Path.cwd(), base, settings.diff_size_cap)
        result = asyncio.run(
            run_review(
                context,
                SdkReviewAgent(settings),
                verify=not no_verify,
                verify_concurrency=settings.verify_concurrency,
            )
        )
    except ArgusError as exc:
        _fail(str(exc))
    if json_path is not None:
        _write_json(json_path, result)
    typer.echo(render_report(result), nl=False)
    if fail_on is not None and gate_tripped(result.review.findings, fail_on):
        raise typer.Exit(EXIT_GATE)


def gate_tripped(findings: list[Finding], fail_on: str) -> bool:
    """True if a confirmed or unverified finding is at or above the severity."""
    threshold = SEVERITY_ORDER[fail_on]
    return any(f.status != "rejected" and SEVERITY_ORDER[f.severity] <= threshold for f in findings)


def _check_credentials() -> None:
    source = credential_source()
    if source is None:
        telemetry.get_logger().info("credential_source", source="machine login")
        return
    if (problem := credential_problem()) is not None:
        _fail(problem)
    telemetry.get_logger().info("credential_source", source=source)


def _write_json(path: Path, result: ReviewResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2) + "\n")
    telemetry.get_logger().info("artifact_written", path=str(path))


def _fail(message: str) -> None:
    telemetry.get_logger().error("run_failed", error=message)
    typer.echo(f"argus: {message}", err=True)
    raise typer.Exit(EXIT_ERROR)
