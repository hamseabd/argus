"""Argus command line."""

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import ValidationError

from argus import __version__, telemetry, tracing
from argus.agent.prompts import prompts_digest
from argus.auth import credential_problem, credential_source
from argus.context.git import head_sha, increment_since, local_context, repo_root
from argus.context.github import GitHubClient, parse_repo, pr_context, repo_from_remote
from argus.context.rules import rules_from_git, rules_from_github
from argus.domain.errors import ArgusError, GitHubError
from argus.domain.models import (
    SEVERITY_ORDER,
    KnownFinding,
    ReviewContext,
    ReviewResult,
    gate_tripped,
)
from argus.pipeline import run_review
from argus.report.check_run import build_check_run
from argus.report.feedback import collect_feedback, render_feedback, summarize_feedback
from argus.report.github_review import build_review, known_findings_from, last_reviewed_head
from argus.report.markdown import render_report
from argus.report.summary import render_summary
from argus.settings import Settings

EXIT_ERROR = 1
EXIT_USAGE = 2  # Click's own code for a bad command line; kept distinct from the gate
EXIT_GATE = 3
STEP_SUMMARY_VAR = "GITHUB_STEP_SUMMARY"

app = typer.Typer(
    help="Argus: a code-review agent on the Claude Agent SDK.",
    no_args_is_help=True,
    add_completion=False,
)


@dataclass(frozen=True)
class PRTarget:
    """Which pull request --pr names and the client that reaches it."""

    client: GitHubClient
    owner: str
    repo: str
    number: int


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
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help=(
                'Post an "Argus" check run on the head with each finding as an annotation; '
                "its conclusion carries the --fail-on gate instead of the exit code (needs --pr)."
            ),
        ),
    ] = False,
    json_path: Annotated[
        Path | None, typer.Option("--json", help="Write the ReviewResult here.")
    ] = None,
    ignore: Annotated[
        list[str] | None,
        typer.Option(
            "--ignore",
            help=(
                "Leave changed files matching this pattern out of the review; repeatable. "
                "A name or glob matches in any directory, `dir/` only at the repository root. "
                "Adds to ARGUS_IGNORE_PATHS."
            ),
        ),
    ] = None,
    include_known: Annotated[
        bool,
        typer.Option(
            "--include-known",
            help=(
                "Report findings an earlier Argus review already posted on the pull request. "
                "Implies --full."
            ),
        ),
    ] = False,
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help=(
                "Review the whole pull request even if Argus reviewed it before; by default "
                "a re-review covers only the commits since the last one."
            ),
        ),
    ] = False,
    summary_path: Annotated[
        Path | None,
        typer.Option(
            "--summary",
            help="Append a Markdown run summary here. Defaults to $GITHUB_STEP_SUMMARY when set.",
        ),
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
    if check and pr is None:
        raise typer.BadParameter("--check needs --pr", param_hint="--check")
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
    target = _github_target(pr, repo) if pr is not None else None
    summary_path = summary_path or _step_summary_from_env()

    with tracing.session():
        from argus.agent.review import SdkReviewAgent  # keep the SDK import lazy

        patterns = (*settings.ignore_patterns, *(ignore or ()))
        known: list[KnownFinding] = []
        try:
            if target is None:
                context = local_context(Path.cwd(), base, settings.diff_size_cap, ignore=patterns)
                rules = rules_from_git(context.repo_root, base)
            else:
                root = repo_root(Path.cwd())
                context = pr_context(
                    target.client,
                    target.owner,
                    target.repo,
                    target.number,
                    root,
                    settings.diff_size_cap,
                    ignore=patterns,
                )
                _warn_on_head_mismatch(context)
                rules = rules_from_github(target.client, target.owner, target.repo)
                if not include_known:
                    reviews = _earlier_reviews(target, settings.reviewer_login)
                    known = _known_findings(target, settings.reviewer_login, reviews)
                    # A gated run reviews everything: an increment would never re-find
                    # an open finding from an earlier push, and the gate would pass.
                    # --check too: a required check must be posted on every head.
                    if not full and fail_on is None and not check:
                        context = _narrow_to_increment(context, reviews, settings, patterns)
            if rules:
                telemetry.get_logger().info("review_rules", chars=len(rules))
                context = context.model_copy(update={"review_rules": rules})
            if not context.files and not context.diff_text.strip():
                _fail(_nothing_to_review(context))
            result = asyncio.run(
                run_review(
                    context,
                    SdkReviewAgent(settings),
                    verify=not no_verify,
                    verify_concurrency=settings.verify_concurrency,
                    run_id=run_id,
                    known=known,
                    config=settings.run_config(prompts_digest()),
                )
            )
        except ArgusError as exc:
            _fail(str(exc))
        if json_path is not None:
            _write_json(json_path, result)
        if summary_path is not None:
            _append_summary(summary_path, render_summary(result, context))
        typer.echo(render_report(result), nl=False)
        # The check run goes first: a required check must be posted on every
        # head, even when posting the review then fails.
        if check and target is not None:
            _post_check_run(target.client, context, result, fail_on)
        if post and target is not None:
            url = _post_review(target.client, context, result)
            if summary_path is not None:
                _append_summary(summary_path, f"Posted: {url}\n")
    # With --check the check run's conclusion is the gate; failing the job as well
    # would make a gated merge read the same as Argus crashing.
    if not check and fail_on is not None and gate_tripped(result.review.findings, fail_on):
        raise typer.Exit(EXIT_GATE)


def _nothing_to_review(context: ReviewContext) -> str:
    if context.ignored_files:
        return (
            "nothing to review: every changed file matches an ignore pattern "
            f"({', '.join(context.ignored_files)}); pass ARGUS_IGNORE_PATHS= to review them"
        )
    return "nothing to review: the diff is empty"


@app.command()
def feedback(
    repo: Annotated[
        str | None,
        typer.Option("--repo", help="owner/name; defaults to GITHUB_REPOSITORY, then origin."),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only findings posted on or after this date (YYYY-MM-DD)."),
    ] = None,
    json_path: Annotated[
        Path | None, typer.Option("--json", help="Write every posted finding here, rated or not.")
    ] = None,
) -> None:
    """Read the reactions on Argus's posted findings: precision as the readers judged it.

    Needs GITHUB_TOKEN and ARGUS_REVIEWER_LOGIN, the login Argus posts as.
    """
    telemetry.configure()
    try:
        settings = Settings()
    except ValidationError as exc:
        _fail(f"invalid ARGUS_* setting: {_settings_problem(exc)}")
    if not settings.reviewer_login:
        _fail("ARGUS_REVIEWER_LOGIN is required: only that login's comments are read")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        _fail("GITHUB_TOKEN is required for feedback")
    slug = repo or os.environ.get("GITHUB_REPOSITORY") or repo_from_remote(Path.cwd())
    if not slug:
        _fail("cannot determine the repository; pass --repo owner/name")
    try:
        owner, name = parse_repo(slug)
    except ValueError as exc:
        _fail(str(exc))
    since_iso = f"{since}T00:00:00Z" if since else None
    try:
        comments = GitHubClient(token).repo_review_comments(owner, name, since=since_iso)
    except GitHubError as exc:
        _fail(str(exc))
    records = collect_feedback(comments, settings.reviewer_login)
    summary = summarize_feedback(records)
    telemetry.get_logger().info(
        "feedback", repo=slug, findings=summary.findings, rated=summary.rated
    )
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "summary": summary.model_dump(mode="json"),
            "findings": [r.model_dump(mode="json") for r in records],
        }
        json_path.write_text(json.dumps(payload, indent=2) + "\n")
    typer.echo(render_feedback(summary), nl=False)


def _earlier_reviews(target: PRTarget, reviewer_login: str) -> list[dict[str, Any]]:
    """The pull request's reviews, fetched only when there is an identity to trust in them."""
    if not reviewer_login:
        # Both read these reviews: without them nothing is known and nothing is incremental.
        telemetry.get_logger().info(
            "earlier_reviews_skipped",
            reason="no_reviewer_login",
            skips=["known_findings", "incremental_review"],
        )
        return []
    return target.client.pr_reviews(target.owner, target.repo, target.number)


def _known_findings(
    target: PRTarget, reviewer_login: str, reviews: list[dict[str, Any]]
) -> list[KnownFinding]:
    """What earlier Argus reviews already posted on the pull request.

    Without a configured login there is no identity to trust, so nothing is
    fetched and every finding is reported; the workflow sets it from the app.
    """
    if not reviewer_login:
        return []
    known = known_findings_from(
        reviews,
        target.client.pr_review_comments(target.owner, target.repo, target.number),
        reviewer_login,
    )
    telemetry.get_logger().info("known_findings", count=len(known), reviewer=reviewer_login)
    return known


def _narrow_to_increment(
    context: ReviewContext,
    reviews: list[dict[str, Any]],
    settings: Settings,
    patterns: tuple[str, ...],
) -> ReviewContext:
    """The context of a re-review: only what changed since the head Argus last reviewed.

    The same head again exits cleanly without a query. A reviewed commit that
    is no longer an ancestor of the head (a force-push) leaves the whole pull
    request to review.
    """
    since = last_reviewed_head(reviews, settings.reviewer_login)
    if since is None or context.pr is None:
        return context
    log = telemetry.get_logger()
    head = context.pr.head_sha
    if since == head:
        log.info("already_reviewed", head=head)
        typer.echo(f"argus: already reviewed at {head[:7]}; nothing new to review")
        raise typer.Exit(0)
    increment = increment_since(
        context.repo_root, since, head, settings.diff_size_cap, ignore=patterns
    )
    if increment is None:
        log.warning("increment_unavailable", since=since, head=head)
        return context
    if not increment.files:
        log.info("nothing_new", since=since, head=head)
        typer.echo(f"argus: nothing reviewable changed since {since[:7]}")
        raise typer.Exit(0)
    log.info("incremental_review", since=since, head=head, files=len(increment.files))
    return context.model_copy(update={"increment": increment})


def _check_credentials() -> None:
    source = credential_source()
    if source is None:
        telemetry.get_logger().info("credential_source", source="machine login")
        return
    if (problem := credential_problem()) is not None:
        _fail(problem)
    telemetry.get_logger().info("credential_source", source=source)


def _github_target(pr: int, repo: str | None) -> PRTarget:
    """The client and pull request for PR mode, or a clean failure before any query."""
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
    return PRTarget(GitHubClient(token), owner, name, pr)


def _warn_on_head_mismatch(context: ReviewContext) -> None:
    """PR mode assumes the working directory is a checkout of the PR head."""
    try:
        local = head_sha(Path.cwd())
    except ArgusError as exc:
        telemetry.get_logger().warning("head_unknown", error=str(exc))
        return
    if context.pr is not None and local != context.pr.head_sha:
        telemetry.get_logger().warning("head_mismatch", local=local, pr_head=context.pr.head_sha)


def _post_review(client: GitHubClient, context: ReviewContext, result: ReviewResult) -> str:
    payload = build_review(result, context)
    if context.pr is None:  # build_review already refused this; keep the type checker happy
        _fail("cannot post a review without a pull request")
    try:
        url = client.post_review(context.pr.owner, context.pr.repo, context.pr.number, payload)
    except GitHubError as exc:
        _fail(str(exc))
    telemetry.get_logger().info("review_posted", url=url, inline=len(payload["comments"]))
    typer.echo(f"Posted review: {url}")
    return url


def _post_check_run(
    client: GitHubClient, context: ReviewContext, result: ReviewResult, fail_on: str | None
) -> None:
    if context.pr is None:
        _fail("cannot post a check run without a pull request")
    payload = build_check_run(result, context, fail_on)
    try:
        url = client.create_check_run(context.pr.owner, context.pr.repo, payload)
    except GitHubError as exc:
        _fail(str(exc))
    telemetry.get_logger().info("check_run_posted", url=url, conclusion=payload["conclusion"])
    typer.echo(f"Check run: {url}")


def _write_json(path: Path, result: ReviewResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2) + "\n")
    telemetry.get_logger().info("artifact_written", path=str(path))


def _step_summary_from_env() -> Path | None:
    """GitHub Actions names the job page's summary file; a run there fills it in unasked."""
    value = os.environ.get(STEP_SUMMARY_VAR, "").strip()
    return Path(value) if value else None


def _append_summary(path: Path, text: str) -> None:
    """Add to the summary file, never replace it: a job's earlier steps may have written there.

    An earlier step may have left the file without a final newline; Markdown
    reads a heading only at the start of a line, so one is supplied then.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    unterminated = path.exists() and path.stat().st_size > 0 and not _ends_with_newline(path)
    with path.open("a", encoding="utf-8") as out:
        if unterminated:
            out.write("\n")
        out.write(text)
    telemetry.get_logger().info("summary_written", path=str(path))


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as existing:
        existing.seek(-1, os.SEEK_END)
        return existing.read(1) == b"\n"


def _settings_problem(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
    )


def _fail(message: str) -> NoReturn:
    # Error text can quote a model's output or a server's reply; it goes to the job log.
    message = tracing.redact(message)
    telemetry.get_logger().error("run_failed", error=message)
    typer.echo(f"argus: {message}", err=True)
    raise typer.Exit(EXIT_ERROR)
