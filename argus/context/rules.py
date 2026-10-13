"""The repository's own review rules, read from a branch the change cannot touch.

A maintainer can write down what a reviewer of this codebase should know:
"money is integer cents", "the ORM escapes every query", "a missing test for
generated code is fine". Argus reads REVIEW.md at the repository root and
hands it to every agent.

Never the change under review: a pull request that edits REVIEW.md, or adds
one, cannot tell the reviewer to look away from itself. Locally the rules
come from --base; for a pull request, from the default branch, since its
author picks the base.
The rules are bounded, so a long file cannot crowd out the diff.
"""

from pathlib import Path

from argus.context.git import run_git
from argus.context.github import GitHubClient
from argus.domain.errors import GitError, GitHubError
from argus.telemetry import get_logger

RULES_PATH = "REVIEW.md"
MAX_RULES_CHARS = 8000
_CUT = f"\n[{RULES_PATH} cut here: it is longer than Argus reads]\n"


def rules_from_git(repo: Path, base: str) -> str | None:
    """REVIEW.md as it is on `base` in a local repository, or None if there is none."""
    try:
        text = run_git(repo, "show", "--end-of-options", f"{base}:{RULES_PATH}", strip=False)
    except GitError:
        return None
    return _bounded(text)


def rules_from_github(client: GitHubClient, owner: str, repo: str) -> str | None:
    """REVIEW.md as it is on the repository's default branch, or None if it cannot be read.

    The default branch, not the pull request's base: the author picks the
    base from any branch they can push to, while the default branch is the
    one the maintainers keep. Any failure (no file, no permission, an outage)
    means no rules: they are an optional brief, never a reason to fail.
    """
    try:
        ref = client.default_branch(owner, repo)
        text = client.file_text(owner, repo, RULES_PATH, ref)
    except GitHubError as exc:
        if exc.status != 404:
            get_logger().warning("review_rules_unreadable", error=str(exc))
        return None
    return _bounded(text)


def _bounded(text: str) -> str | None:
    if not text.strip():
        return None
    if len(text) <= MAX_RULES_CHARS:
        return text
    return text[:MAX_RULES_CHARS] + _CUT
