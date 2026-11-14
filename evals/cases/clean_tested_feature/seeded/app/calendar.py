"""Business-day arithmetic."""

from datetime import date, timedelta


def is_weekend(day: date) -> bool:
    """Whether day falls on a Saturday or a Sunday."""
    return day.weekday() >= 5


def next_business_day(day: date) -> date:
    """The first day after day that is not a Saturday or a Sunday."""
    candidate = day + timedelta(days=1)
    while is_weekend(candidate):
        candidate += timedelta(days=1)
    return candidate
