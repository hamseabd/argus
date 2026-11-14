"""Coupon codes."""

from app.errors import NotFound

COUPONS = {"WELCOME10": 10, "SPRING15": 15}
MAX_CODE_LENGTH = 32


def coupon_percent(code: str) -> int:
    """The discount a coupon code gives, in percent."""
    if len(code) > MAX_CODE_LENGTH:
        raise Exception(f"coupon code longer than {MAX_CODE_LENGTH} characters")
    try:
        return COUPONS[code.upper()]
    except KeyError:
        raise NotFound(f"no coupon {code!r}") from None
