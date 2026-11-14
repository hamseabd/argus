from app.cart import line_total


def test_a_line_total_multiplies_price_by_quantity() -> None:
    assert line_total(250, 4) == 1000
