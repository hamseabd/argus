from datetime import date

import pytest

from app.calendar import is_weekend


@pytest.mark.parametrize(
    ("day", "weekend"),
    [(date(2026, 10, 23), False), (date(2026, 10, 24), True), (date(2026, 10, 25), True)],
)
def test_saturday_and_sunday_are_the_weekend(day: date, weekend: bool) -> None:
    assert is_weekend(day) is weekend
