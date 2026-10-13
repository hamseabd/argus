"""The repository's own review rules, read from the base branch.

A maintainer can write down what a reviewer of this codebase should know:
"money is integer cents", "the ORM escapes every query", "a missing test for
generated code is fine". Argus reads REVIEW.md at the root of the base
branch and hands it to every agent.

Always the base, never the change under review: a pull request that edits
REVIEW.md, or adds one, cannot tell the reviewer to look away from itself.
The rules are bounded, so a long file cannot crowd out the diff.
"""

from pathlib import Path

from argus.context.git import run_git
from argus.context.github import GitHubClient
from argus.domain.errors import GitError, GitHubError

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


def rules_from_github(client: GitHubClient, owner: str, repo: str, ref: str) -> str | None:
    """REVIEW.md as it is on `ref` on GitHub, or None if there is none."""
    try:
        text = client.file_text(owner, repo, RULES_PATH, ref)
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise
    return _bounded(text)


def _bounded(text: str) -> str | None:
    if not text.strip():
        return None
    if len(text) <= MAX_RULES_CHARS:
        return text
    return text[:MAX_RULES_CHARS] + _CUT
