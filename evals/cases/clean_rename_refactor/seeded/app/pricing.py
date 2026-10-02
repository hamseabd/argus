"""Totals for a shopping cart, in integer cents."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Line:
    unit_cents: int
    quantity: int


def subtotal_cents(lines: list[Line]) -> int:
    """Sum of unit price times quantity over every line."""
    return sum(line.unit_cents * line.quantity for line in lines)


def total_with_tax_cents(lines: list[Line], rate_percent: int) -> int:
    subtotal = subtotal_cents(lines)
    return subtotal + (subtotal * rate_percent + 50) // 100
