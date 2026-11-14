"""Recording payments."""

from app.log import get_logger


def record_payment(ledger: list[tuple[str, int]], payment_id: str, amount_cents: int) -> None:
    """Append a payment to the ledger."""
    ledger.append((payment_id, amount_cents))
    get_logger().info("payment recorded")
