"""Count error lines across many log files."""

from pathlib import Path


def _error_lines(path: Path) -> int:
    handle = open(path, encoding="utf-8")
    return sum("ERROR" in line for line in handle)


def count_errors(paths: list[Path]) -> int:
    """Lines containing ERROR across every file in paths."""
    return sum(_error_lines(path) for path in paths)
