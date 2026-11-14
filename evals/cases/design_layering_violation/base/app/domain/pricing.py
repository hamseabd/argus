"""Price rules."""


def loyalty_discount_percent(years: int) -> int:
    """One percent per year of loyalty, up to ten."""
    return min(years, 10)
