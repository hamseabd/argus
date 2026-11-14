from app.discount import apply_discount


def test_a_discount_takes_its_percentage_off() -> None:
    assert apply_discount(1000, 10) == 900


def test_a_discount_is_capped_at_half_the_price() -> None:
    expected = apply_discount(1000, 80)
    assert apply_discount(1000, 80) == expected
