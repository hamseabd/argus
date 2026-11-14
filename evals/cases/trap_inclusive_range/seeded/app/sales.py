"""Sales totals keyed by day of the month, numbered from 1."""

DAYS_PER_WEEK = 7


def day_total(totals: dict[int, int], day: int) -> int:
    """Cents sold on one day; a day with no sales counts as zero."""
    return totals.get(day, 0)


def span_total(totals: dict[int, int], start: int, end: int) -> int:
    """Cents sold from day start to day end, both days inclusive."""
    return sum(day_total(totals, day) for day in range(start, end + 1))


def week_total(totals: dict[int, int], week: int) -> int:
    """Cents sold in a week of the month; week 1 is days 1 to 7."""
    first_day = (week - 1) * DAYS_PER_WEEK + 1
    return span_total(totals, first_day, first_day + DAYS_PER_WEEK - 1)
