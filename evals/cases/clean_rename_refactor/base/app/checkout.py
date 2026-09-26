"""Checkout summary."""

from app.pricing import Line, calc, calc_with_tax


def summary(lines: list[Line], rate_percent: int) -> dict[str, int]:
    return {"subtotal": calc(lines), "total": calc_with_tax(lines, rate_percent)}
