"""Invoices."""

from app.tax import with_vat


def invoice_total(line_cents: list[int]) -> int:
    """The gross total of an invoice."""
    return with_vat(sum(line_cents))


def credit_note_total(line_cents: list[int]) -> int:
    """The gross total of a credit note, as a negative amount."""
    net = sum(line_cents)
    gross = (net * 120 + 50) // 100
    return -gross
