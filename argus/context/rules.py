"""The repository's own review rules, read from a branch the change cannot touch.

REVIEW.md says what the repository counts as a finding: its standard, its
layers, paths to skip, its nit policy, extra checks. CLAUDE.md carries its
conventions. Both go to every agent as fenced text in its prompt;
setting_sources=[] still keeps the reviewer's own session from loading any
of the repository's settings.

Never the change under review: a pull request that edits either file, or
adds one, cannot tell the reviewer to look away from itself. Locally the
rules come from --base; for a pull request, from the default branch, since
its author picks the base from any branch they can push to. A missing file
means the defaults. The rules are bounded, so a long file cannot crowd out
the diff.
"""

from collections.abc import Callable
from pathlib import Path

from argus.context.git import run_git
from argus.context.github import GitHubClient
from argus.domain.errors import GitError, GitHubError
from argus.domain.models import RepoRules
from argus.telemetry import get_logger

RULE_FILES: tuple[str, ...] = ("REVIEW.md", "CLAUDE.md")
MAX_RULES_CHARS = 8000
"""Characters kept from one rule file; the rest is cut and marked as cut."""


def collect_rules(ref: str, fetch: Callable[[str], str | None]) -> RepoRules:
    """Fetch each rule file at ref, skip the missing and empty ones, and bound the rest."""
    files = {}
    for path in RULE_FILES:
        text = fetch(path)
        if text is None or not text.strip():
            continue
        if len(text) > MAX_RULES_CHARS:
            text = f"{text[:MAX_RULES_CHARS]}\n[{path} cut here: it is longer than Argus reads]\n"
        files[path] = text
    get_logger().info("review_rules", ref=ref, files=list(files))
    return RepoRules(ref=ref, files=files)


def rules_from_git(repo: Path, base: str) -> RepoRules:
    """The rule files as committed on `base` in a local repository; never the working tree."""

    def fetch(path: str) -> str | None:
        try:
            return run_git(repo, "show", "--end-of-options", f"{base}:{path}", strip=False)
        except GitError:
            return None

    return collect_rules(base, fetch)


def rules_from_github(client: GitHubClient, owner: str, repo: str) -> RepoRules | None:
    """The rule files on the repository's default branch, or None if it cannot be found.

    Any failure to read a file (no file, no permission, an outage) means that
    file is left out: the rules are an optional brief, never a reason to fail.
    """
    try:
        ref = client.default_branch(owner, repo)
    except GitHubError as exc:
        get_logger().warning("review_rules_unreadable", error=str(exc))
        return None

    def fetch(path: str) -> str | None:
        try:
            return client.file_text(owner, repo, path, ref)
        except GitHubError as exc:
            if exc.status != 404:
                get_logger().warning("review_rules_unreadable", path=path, error=str(exc))
            return None

    return collect_rules(ref, fetch)
