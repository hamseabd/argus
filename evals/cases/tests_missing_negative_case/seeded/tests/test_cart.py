from app.cart import line_total, parse_quantity


def test_a_line_total_multiplies_price_by_quantity() -> None:
    assert line_total(250, 4) == 1000


def test_a_typed_quantity_is_parsed() -> None:
    assert parse_quantity(" 3 ") == 3
