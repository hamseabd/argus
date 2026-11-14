"""Read-only questions about a checked-out repository."""

import subprocess
from pathlib import Path


def current_branch(repo: Path) -> str:
    """The branch the repository has checked out."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
