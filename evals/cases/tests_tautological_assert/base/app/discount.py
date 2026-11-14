"""Discounts applied at checkout."""


def apply_discount(price_cents: int, percent: int) -> int:
    """The price after a percentage discount, rounded down to the cent."""
    return price_cents * (100 - percent) // 100
