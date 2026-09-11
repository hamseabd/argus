"""Argus command line."""

import asyncio
import os
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError

from argus import __version__, telemetry
from argus.auth import credential_problem, credential_source
from argus.context.git import head_sha, local_context, repo_root
from argus.context.github import GitHubClient, parse_repo, pr_context, repo_from_remote
from argus.domain.errors import ArgusError, GitHubError
from argus.domain.models import SEVERITY_ORDER, Finding, ReviewContext, ReviewResult
from argus.pipeline import run_review
from argus.report.github_review import build_review
from argus.report.markdown import render_report
from argus.settings import Settings

EXIT_ERROR = 1
EXIT_USAGE = 2  # Click's own code for a bad command line; kept distinct from the gate
EXIT_GATE = 3

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
    pr: Annotated[int | None, typer.Option("--pr", help="Review this pull request number.")] = None,
    diff: Annotated[
        bool, typer.Option("--diff", help="Review the local diff against --base.")
    ] = False,
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo", help="owner/name for --pr; defaults to GITHUB_REPOSITORY, then origin."
        ),
    ] = None,
    base: Annotated[str, typer.Option("--base", help="Base ref for --diff.")] = "main",
    post: Annotated[
        bool, typer.Option("--post", help="Post the review on the pull request (needs --pr).")
    ] = False,
    json_path: Annotated[
        Path | None, typer.Option("--json", help="Write the ReviewResult here.")
    ] = None,
    no_verify: Annotated[bool, typer.Option("--no-verify", help="Skip the verify stage.")] = False,
    fail_on: Annotated[
        str | None,
        typer.Option(
            "--fail-on", help="Exit 3 if any reported finding is at or above this severity."
        ),
    ] = None,
) -> None:
    """Review a change and print the findings.

    Exit codes: 0 success, 1 error, 2 bad command line, 3 severity gate tripped.
    """
    if (pr is None) == (not diff):
        raise typer.BadParameter("choose one mode: --pr <number> or --diff")
    if post and pr is None:
        raise typer.BadParameter("--post needs --pr", param_hint="--post")
    if fail_on is not None and fail_on not in SEVERITY_ORDER:
        raise typer.BadParameter(
            f"{fail_on!r} is not a severity; expected one of {', '.join(SEVERITY_ORDER)}",
            param_hint="--fail-on",
        )
    telemetry.configure()
    run_id = telemetry.new_run_id()
    telemetry.bind_run(run_id=run_id)
    try:
        settings = Settings()
    except ValidationError as exc:
        _fail(f"invalid ARGUS_* setting: {_settings_problem(exc)}")
    telemetry.configure(settings.log_format, level=settings.log_level)
    telemetry.bind_run(run_id=run_id)
    _check_credentials()
    github = _github_target(pr, repo) if pr is not None else None

    from argus.agent.review import SdkReviewAgent  # keep the SDK import lazy

    try:
        if github is None:
            context = local_context(Path.cwd(), base, settings.diff_size_cap)
        else:
            client, owner, name = github
            root = repo_root(Path.cwd())
            context = pr_context(client, owner, name, pr, root, settings.diff_size_cap)
            _warn_on_head_mismatch(context)
        if not context.files and not context.diff_text.strip():
            _fail("nothing to review: the diff is empty")
        result = asyncio.run(
            run_review(
                context,
                SdkReviewAgent(settings),
                verify=not no_verify,
                verify_concurrency=settings.verify_concurrency,
                run_id=run_id,
            )
        )
    except ArgusError as exc:
        _fail(str(exc))
    if json_path is not None:
        _write_json(json_path, result)
    typer.echo(render_report(result), nl=False)
    if post and github is not None:
        _post_review(github[0], context, result)
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


def _github_target(pr: int, repo: str | None) -> tuple[GitHubClient, str, str]:
    """The client and owner/name for PR mode, or a clean failure before any query."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        _fail("GITHUB_TOKEN is required for --pr")
    slug = repo or os.environ.get("GITHUB_REPOSITORY") or repo_from_remote(Path.cwd())
    if not slug:
        _fail("cannot determine the repository; pass --repo owner/name")
    try:
        owner, name = parse_repo(slug)
    except ValueError as exc:
        _fail(str(exc))
    telemetry.get_logger().info("pr_target", repo=slug, pr=pr)
    return GitHubClient(token), owner, name


def _warn_on_head_mismatch(context: ReviewContext) -> None:
    """PR mode assumes the working directory is a checkout of the PR head."""
    try:
        local = head_sha(Path.cwd())
    except ArgusError as exc:
        telemetry.get_logger().warning("head_unknown", error=str(exc))
        return
    if context.pr is not None and local != context.pr.head_sha:
        telemetry.get_logger().warning("head_mismatch", local=local, pr_head=context.pr.head_sha)


def _post_review(client: GitHubClient, context: ReviewContext, result: ReviewResult) -> None:
    payload = build_review(result, context)
    if context.pr is None:  # build_review already refused this; keep the type checker happy
        _fail("cannot post a review without a pull request")
    try:
        url = client.post_review(context.pr.owner, context.pr.repo, context.pr.number, payload)
    except GitHubError as exc:
        _fail(str(exc))
    telemetry.get_logger().info("review_posted", url=url, inline=len(payload["comments"]))
    typer.echo(f"Posted review: {url}")


def _write_json(path: Path, result: ReviewResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2) + "\n")
    telemetry.get_logger().info("artifact_written", path=str(path))


def _settings_problem(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
    )


def _fail(message: str) -> NoReturn:
    telemetry.get_logger().error("run_failed", error=message)
    typer.echo(f"argus: {message}", err=True)
    raise typer.Exit(EXIT_ERROR)
