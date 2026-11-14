"""Sales tax."""

VAT_RATE_PERCENT = 20


def with_vat(net_cents: int) -> int:
    """The gross amount: net plus VAT, rounded half up to the cent."""
    return (net_cents * (100 + VAT_RATE_PERCENT) + 50) // 100
