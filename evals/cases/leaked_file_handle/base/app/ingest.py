"""Count error lines across many log files."""

from pathlib import Path


def count_errors(paths: list[Path]) -> int:
    """Lines containing ERROR across every file in paths."""
    total = 0
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            total += sum("ERROR" in line for line in handle)
    return total
