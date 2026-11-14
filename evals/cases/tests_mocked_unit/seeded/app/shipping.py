"""Shipping prices by parcel weight."""

HEAVY_PARCEL_KG = 20


def shipping_cost(weight_kg: float) -> int:
    """The shipping price in cents for a parcel of this weight."""
    if weight_kg <= 1:
        return 500
    if weight_kg > HEAVY_PARCEL_KG:
        return 2500
    return 900
