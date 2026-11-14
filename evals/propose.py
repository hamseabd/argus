"""Propose a corpus case from a real Argus finding.

    uv run python -m evals.propose <owner>/<name>#<pr> <comment id> [--base <sha>]

The case is the pull request as Argus reviewed it: `base/` holds every file
the pull request changes at its merge base, `seeded/` the same files at the
commit the finding was posted on. A pull request stacked on another branch,
or rebased since the review, has a merge base far from what Argus saw; pass
the commit the review was made against as `base` then. Either way only the
files the pull request changes are taken. expected.yaml is prefilled from the
finding and its reactions: more 👎 than 👍 makes a clean control (the
finding was wrong, so the change should draw nothing), anything else a bug
case at the finding's lines, owned by the specialist that reported it, as
feedback reads it: the review's handoff, else the comment's "found by".

The command only writes files. A case joins the corpus when Hamse says so and
it is committed by hand, after its lines and its kind have been checked: a
wrong expectation in the corpus does more harm than a small corpus.
"""

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any, Protocol

import typer
import yaml
from pydantic import ValidationError

from argus import telemetry
from argus.context.github import GitHubClient
from argus.domain.errors import ArgusError
from argus.domain.models import ChangedFile, PRInfo
from argus.report.feedback import handoff_record, is_false_positive, pr_number_of, reporter_of
from argus.report.github_review import inline_header, posted_by
from argus.settings import Settings
from argus.tracing import redact
from evals.corpus import CASES_DIR, CLEAN_PREFIX

_TARGET = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)#(?P<number>[1-9]\d*)$")
_UNSAFE_NAME = re.compile(r"[^a-z0-9]+")


class ProposeError(ArgusError):
    """The finding cannot become a case: what was asked for is not an Argus finding, or unsafe."""


class GitHubReader(Protocol):
    def review_comment(self, owner: str, repo: str, comment_id: int) -> dict[str, Any]: ...
    def pr_info(self, owner: str, repo: str, number: int) -> PRInfo: ...
    def pr_files(self, owner: str, repo: str, number: int) -> list[ChangedFile]: ...
    def compare(self, owner: str, repo: str, base: str, head: str) -> dict[str, Any]: ...
    def file_text(self, owner: str, repo: str, path: str, ref: str) -> str: ...
    def pr_reviews(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]: ...


def parse_target(value: str) -> tuple[str, str, int]:
    """owner/name#number, as GitHub writes a pull request reference."""
    match = _TARGET.match(value)
    if match is None:
        raise ProposeError(f"expected <owner>/<name>#<pull request>, got {value!r}")
    return match["owner"], match["repo"], int(match["number"])


def propose_case(
    github: GitHubReader,
    owner: str,
    repo: str,
    number: int,
    comment_id: int,
    *,
    reviewer_login: str,
    base: str | None = None,
    cases_dir: Path | None = None,
) -> Path:
    """Write the case directory for one finding comment and return it.

    The case is built in a staging directory and moved into place only once
    it is whole, so a failure leaves nothing behind to block a retry or to
    break the corpus.
    """
    comment = github.review_comment(owner, repo, comment_id)
    body = str(comment.get("body") or "")
    header = inline_header(body)
    if not reviewer_login:
        raise ProposeError("ARGUS_REVIEWER_LOGIN is required: only that login's findings count")
    if header is None or not posted_by(comment.get("user"), reviewer_login):
        raise ProposeError(f"comment {comment_id} is not an Argus finding by {reviewer_login}")
    if (on_pr := pr_number_of(comment)) != number:
        found = on_pr if on_pr is not None else "unknown"
        raise ProposeError(f"comment {comment_id} is on pull request {found}, not {number}")
    reactions = comment.get("reactions") or {}
    wrong = is_false_positive(int(reactions.get("+1") or 0), int(reactions.get("-1") or 0))
    name = (CLEAN_PREFIX if wrong else "") + _UNSAFE_NAME.sub("_", repo.lower()).strip("_")
    case = (cases_dir or CASES_DIR) / f"{name}_pr{number}_c{comment_id}"
    if case.exists():
        raise ProposeError(f"{case} already exists")
    reviewed = str(comment["original_commit_id"])
    since = base or github.pr_info(owner, repo, number).base_sha
    compared = github.compare(owner, repo, since, reviewed)
    changed = {
        path
        for f in github.pr_files(owner, repo, number)
        for path in (f.path, f.previous_path)
        if path
    }
    record = handoff_record(comment, github.pr_reviews(owner, repo, number), reviewer_login)
    specialist = reporter_of(comment, record)
    case.parent.mkdir(parents=True, exist_ok=True)
    # Beside the corpus, not in it: a leftover would be read as a case.
    with tempfile.TemporaryDirectory(dir=case.parent.parent, prefix=".propose-") as tmp:
        staged = Path(tmp) / case.name
        for target, path, ref in _snapshots(compared, staged, reviewed, changed):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(github.file_text(owner, repo, path, ref), encoding="utf-8")
        expected = _expected(comment, header, specialist, wrong)
        (staged / "expected.yaml").write_text(_header(comment, header) + expected, encoding="utf-8")
        shutil.move(staged, case)
    return case


def _snapshots(
    compared: dict[str, Any], case: Path, reviewed: str, changed: set[str]
) -> list[tuple[Path, str, str]]:
    """(where to write, which path, at which commit) for both sides of each file the PR changes.

    Every path is checked before anything is written: a path from the API
    that would land outside the case directory refuses the whole case.
    """
    merge_base = str(compared["merge_base_commit"]["sha"])
    snapshots = []
    for item in compared.get("files", []):
        path = str(item["filename"])
        if path not in changed and item.get("previous_filename") not in changed:
            continue
        status = item.get("status")
        if status != "added":
            before = str(item.get("previous_filename") or path)
            snapshots.append((_inside(case / "base", before), before, merge_base))
        if status != "removed":
            snapshots.append((_inside(case / "seeded", path), path, reviewed))
    return snapshots


def _inside(root: Path, path: str) -> Path:
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ProposeError(f"{path} would be written outside the case directory")
    return target


def _expected(
    comment: dict[str, Any], header: dict[str, str], specialist: str | None, wrong: bool
) -> str:
    if wrong:
        return yaml.safe_dump({"kind": "clean"}, sort_keys=False)
    end = int(comment.get("original_line") or comment["line"])
    start = int(comment.get("original_start_line") or end)
    bug: dict[str, Any] = {
        "file": str(comment["path"]),
        "line_start": start,
        "line_end": end,
        "category": header["category"],
        "severity": header["severity"],
    }
    if specialist:
        bug["specialist"] = specialist
    return yaml.safe_dump({"kind": "bug", "bugs": [bug]}, sort_keys=False)


def _header(comment: dict[str, Any], header: dict[str, str]) -> str:
    """Where the case came from, as YAML comments: check every line before committing it."""
    reactions = comment.get("reactions") or {}
    title = " ".join(header["title"].split())
    return (
        f"# Proposed from {comment.get('html_url')}\n"
        f"# Finding: {title}\n"
        f"# Reactions: +1 {reactions.get('+1') or 0}, -1 {reactions.get('-1') or 0}. "
        "Check the lines and the kind before committing.\n"
    )


app = typer.Typer(add_completion=False)


@app.command()
def main(
    target: Annotated[str, typer.Argument(help="The pull request, as <owner>/<name>#<number>.")],
    comment_id: Annotated[int, typer.Argument(help="The id of Argus's inline finding comment.")],
    base: Annotated[
        str | None,
        typer.Option(
            "--base",
            help=(
                "The commit the review was made against, for a pull request stacked on "
                "another branch or rebased since. Default: the merge base."
            ),
        ),
    ] = None,
) -> None:
    """Snapshot a real finding's pull request into a new eval case. Never commits.

    Read-only on GitHub. Needs GITHUB_TOKEN and ARGUS_REVIEWER_LOGIN.
    """
    telemetry.configure()
    log = telemetry.get_logger()
    try:
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            raise ProposeError("GITHUB_TOKEN is required")
        owner, name, number = parse_target(target)
        case = propose_case(
            GitHubClient(token),
            owner,
            name,
            number,
            comment_id,
            reviewer_login=Settings().reviewer_login,
            base=base,
        )
    except (ArgusError, ValidationError) as exc:
        message = redact(str(exc))
        log.error("propose_failed", error=message)
        typer.echo(f"propose: {message}", err=True)
        raise typer.Exit(1) from exc
    log.info("case_proposed", case=str(case), source=target, comment=comment_id)
    typer.echo(
        f"Wrote {case}: not committed. Check expected.yaml against the snapshots, "
        "run the corpus tests, and add it only once Hamse agrees."
    )


if __name__ == "__main__":
    app()
