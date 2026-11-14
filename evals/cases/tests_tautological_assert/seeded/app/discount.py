"""Discounts applied at checkout."""

MAX_DISCOUNT_PERCENT = 50


def apply_discount(price_cents: int, percent: int) -> int:
    """The price after a percentage discount, capped at MAX_DISCOUNT_PERCENT."""
    percent = min(percent, MAX_DISCOUNT_PERCENT)
    return price_cents * (100 - percent) // 100
