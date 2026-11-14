"""Sales totals keyed by day of the month, numbered from 1."""

DAYS_PER_WEEK = 7


def day_total(totals: dict[int, int], day: int) -> int:
    """Cents sold on one day; a day with no sales counts as zero."""
    return totals.get(day, 0)
