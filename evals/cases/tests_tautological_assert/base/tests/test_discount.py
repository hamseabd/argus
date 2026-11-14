from app.discount import apply_discount


def test_a_discount_takes_its_percentage_off() -> None:
    assert apply_discount(1000, 10) == 900
