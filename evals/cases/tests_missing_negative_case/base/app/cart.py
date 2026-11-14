"""The shopping cart."""


def line_total(unit_cents: int, quantity: int) -> int:
    """The total for one cart line."""
    return unit_cents * quantity
