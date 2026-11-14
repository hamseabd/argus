"""Shipping prices by parcel weight."""


def shipping_cost(weight_kg: float) -> int:
    """The shipping price in cents for a parcel of this weight."""
    if weight_kg <= 1:
        return 500
    return 900
