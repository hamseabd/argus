"""Read one file of the repository under review, for the verifier.

The path comes from a finding, which the model wrote, so it is untrusted:
it is resolved, symlinks included, and read only when it lands inside the
repository root and outside .git. Anything else, a missing file, or a file over the size
cap reads as None, and the verifier is told to read the file itself.
"""

from pathlib import Path

from argus.telemetry import get_logger

SOURCE_SIZE_CAP = 100_000
"""Bytes of one file put in a verifier prompt; about 2,500 lines of code."""


def within_repository(root: Path, target: Path) -> bool:
    """Whether target, already resolved, lies inside root and outside its .git.

    A checkout can keep a token in .git/config. .git is matched in any case:
    on a case-insensitive filesystem, the default on macOS and Windows, .GIT
    is the same directory, and resolving a path keeps the case it was given.
    """
    if not target.is_relative_to(root):
        return False
    return all(part.casefold() != ".git" for part in target.relative_to(root).parts)


def read_source(repo_root: Path, path: str, max_bytes: int = SOURCE_SIZE_CAP) -> str | None:
    root = repo_root.resolve()
    target = (root / path).resolve()
    if not within_repository(root, target):
        get_logger().warning("source_outside_repo", path=path)
        return None
    try:
        if not target.is_file() or target.stat().st_size > max_bytes:
            return None
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        get_logger().warning("source_unreadable", path=path, error=str(exc))
        return None
