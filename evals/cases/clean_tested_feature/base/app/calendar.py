"""Business-day arithmetic."""

from datetime import date


def is_weekend(day: date) -> bool:
    """Whether day falls on a Saturday or a Sunday."""
    return day.weekday() >= 5
