"""Exporting orders to CSV."""

import csv
import io

from app.log import get_logger


def export_orders(rows: list[tuple[int, int]]) -> str:
    """Orders as CSV text: one id,total_cents row per order."""
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    get_logger().info("orders exported", extra={"rows": len(rows)})
    return out.getvalue()
