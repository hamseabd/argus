from datetime import date

import pytest

from app.calendar import is_weekend, next_business_day


@pytest.mark.parametrize(
    ("day", "weekend"),
    [(date(2026, 10, 23), False), (date(2026, 10, 24), True), (date(2026, 10, 25), True)],
)
def test_saturday_and_sunday_are_the_weekend(day: date, weekend: bool) -> None:
    assert is_weekend(day) is weekend


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 21), date(2026, 10, 22)),  # Wednesday to Thursday
        (date(2026, 10, 23), date(2026, 10, 26)),  # Friday skips the weekend
        (date(2026, 10, 24), date(2026, 10, 26)),  # Saturday to Monday
        (date(2026, 10, 25), date(2026, 10, 26)),  # Sunday to Monday
    ],
)
def test_the_next_business_day_skips_the_weekend(day: date, expected: date) -> None:
    assert next_business_day(day) == expected
