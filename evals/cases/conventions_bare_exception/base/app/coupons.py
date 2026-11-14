"""Coupon codes."""

from app.errors import NotFound

COUPONS = {"WELCOME10": 10, "SPRING15": 15}


def coupon_percent(code: str) -> int:
    """The discount a coupon code gives, in percent."""
    try:
        return COUPONS[code.upper()]
    except KeyError:
        raise NotFound(f"no coupon {code!r}") from None
