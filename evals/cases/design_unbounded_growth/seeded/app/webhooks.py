"""Handling webhook deliveries from the payment provider.

The provider retries a delivery until it is acknowledged, so the same
delivery can arrive more than once; a repeat is acknowledged and skipped.
This process runs for weeks between deploys.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Delivery:
    id: str
    event: str


_seen_delivery_ids: set[str] = set()


def handle(delivery: Delivery) -> str:
    """Process one delivery and say what was done with it."""
    if delivery.id in _seen_delivery_ids:
        return "duplicate"
    _seen_delivery_ids.add(delivery.id)
    return f"processed {delivery.event}"
