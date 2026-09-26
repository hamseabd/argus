"""Checkout summary."""

from app.pricing import Line, subtotal_cents, total_with_tax_cents


def summary(lines: list[Line], rate_percent: int) -> dict[str, int]:
    return {"subtotal": subtotal_cents(lines), "total": total_with_tax_cents(lines, rate_percent)}
