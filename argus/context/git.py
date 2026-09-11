"""Local diff via git.

Local mode reviews the branch the user is on: every commit since it forked
from the base branch, plus whatever is staged or modified in the working
tree. Untracked files are not included, because git diff does not see them
and Argus never touches the index of the repository it reviews.
"""

import subprocess
from pathlib import Path

from argus.context.diff import DIFF_SIZE_CAP, cap_diff, parse_diff
from argus.domain.errors import GitError
from argus.domain.models import ReviewContext

DEFAULT_BASE = "main"


def repo_root(path: Path) -> Path:
    """The top-level directory of the repository containing path."""
    return Path(_git(path, "rev-parse", "--show-toplevel")).resolve()


def head_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def local_diff(repo: Path, base: str = DEFAULT_BASE) -> str:
    """Unified diff from the merge base with `base` to the working tree.

    Diffing from the merge base rather than from `base` itself means commits
    that landed on the base branch after the fork point do not show up as
    changes under review. Rename detection is on so a moved file is one
    section rather than a delete and an add.
    """
    merge_base = _git(repo, "merge-base", "--end-of-options", base, "HEAD")
    return _git(
        repo,
        # Pin the output shape so a user's diff.noprefix, diff.mnemonicPrefix,
        # or core.quotePath settings cannot change what the parser sees.
        "-c",
        "core.quotePath=false",
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "-M",
        merge_base,
        strip=False,
    )


def local_context(
    path: Path, base: str = DEFAULT_BASE, max_bytes: int = DIFF_SIZE_CAP
) -> ReviewContext:
    """Build the review context for the repository containing path."""
    root = repo_root(path)
    files = parse_diff(local_diff(root, base))
    diff_text, truncated = cap_diff(files, max_bytes)
    return ReviewContext(
        source="local",
        repo_root=root,
        diff_text=diff_text,
        files=[f.to_changed_file() for f in files],
        truncated_files=truncated,
    )


def _git(cwd: Path, *args: str, strip: bool = True) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:  # git missing or cwd unusable
        raise GitError(f"could not run git {args[0]}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit status {completed.returncode}"
        raise GitError(f"git {_subcommand(args)} failed: {detail}")
    return completed.stdout.strip() if strip else completed.stdout


def _subcommand(args: tuple[str, ...]) -> str:
    """The git verb in args, skipping any leading -c key=value pairs."""
    i = 0
    while i < len(args) and args[i] == "-c":
        i += 2
    return args[i] if i < len(args) else "?"
