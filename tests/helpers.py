"""Shared test helpers."""

import shutil
import subprocess
from pathlib import Path

SEEDED_FIXTURE = Path(__file__).parent / "fixtures" / "seeded_bug_repo"
SEEDED_FILES = ("app/repo.py", "app/paging.py")
"""Files the seeded version changes: a SQL injection and an off-by-one."""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def build_seeded_repo(target: Path) -> Path:
    """A git repo whose main holds the clean fixture and whose feature branch adds the bugs.

    The working tree is left on `feature` with the seeded files committed, so
    `argus review --diff --base main` sees exactly the seeded change.
    """
    target.mkdir(parents=True, exist_ok=True)
    git(target, "init", "-q", "-b", "main")
    git(target, "config", "user.email", "fixture@argus.test")
    git(target, "config", "user.name", "Fixture")
    shutil.copytree(SEEDED_FIXTURE / "base", target, dirs_exist_ok=True)
    git(target, "add", "-A")
    git(target, "commit", "-q", "-m", "feat: add user lookups and paging")
    git(target, "checkout", "-q", "-b", "feature")
    shutil.copytree(SEEDED_FIXTURE / "seeded", target, dirs_exist_ok=True)
    git(target, "add", "-A")
    git(target, "commit", "-q", "-m", "refactor: simplify the query and the slice")
    return target
