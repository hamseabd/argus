"""Summary statistics for the monthly sales report."""

from collections.abc import Sequence
from typing import Any

Row = dict[str, Any]


def total(rows: Sequence[Row]) -> int:
    """Sum of every row's amount."""
    return sum(row["amount"] for row in rows)


def average(rows: Sequence[Row]) -> float:
    """Mean amount per row; 0.0 for a month with no sales."""
    return total(rows) / len(rows)


def largest(rows: Sequence[Row]) -> Row | None:
    """The row with the largest amount, or None for an empty month."""
    return max(rows, key=lambda row: row["amount"], default=None)


def smallest(rows: Sequence[Row]) -> Row | None:
    """The row with the smallest amount, or None for an empty month."""
    return min(rows, key=lambda row: row["amount"], default=None)


def by_region(rows: Sequence[Row]) -> dict[str, list[Row]]:
    """Rows grouped by their region code."""
    groups: dict[str, list[Row]] = {}
    for row in rows:
        groups.setdefault(row["region"], []).append(row)
    return groups


def region_totals(rows: Sequence[Row]) -> dict[str, int]:
    """Total amount per region."""
    return {region: total(group) for region, group in by_region(rows).items()}


def top_regions(rows: Sequence[Row], count: int) -> list[str]:
    """The count regions with the highest totals, highest first."""
    totals = region_totals(rows)
    return sorted(totals, key=totals.__getitem__, reverse=True)[:count]


def share(rows: Sequence[Row], region: str) -> float:
    """A region's share of the month's total, between 0 and 1."""
    overall = total(rows)
    if overall == 0:
        return 0.0
    return region_totals(rows).get(region, 0) / overall


def format_cents(amount: float) -> str:
    """An amount in cents as a dollar string."""
    return f"${amount / 100:,.2f}"


def summary_line(rows: Sequence[Row]) -> str:
    """One line for the report header."""
    return f"{len(rows)} sales, {format_cents(total(rows))} total, {format_cents(average(rows))} average"
