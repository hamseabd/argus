"""Invoices."""

from app.tax import with_vat


def invoice_total(line_cents: list[int]) -> int:
    """The gross total of an invoice."""
    return with_vat(sum(line_cents))
