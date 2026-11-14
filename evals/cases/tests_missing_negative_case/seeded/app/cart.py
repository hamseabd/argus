"""The shopping cart."""


def line_total(unit_cents: int, quantity: int) -> int:
    """The total for one cart line."""
    return unit_cents * quantity


def parse_quantity(text: str) -> int:
    """The quantity a customer typed, as a positive whole number.

    Raises ValueError for anything that is not a whole number of at least one.
    """
    quantity = int(text.strip())
    if quantity < 1:
        raise ValueError(f"quantity must be at least 1, got {quantity}")
    return quantity
