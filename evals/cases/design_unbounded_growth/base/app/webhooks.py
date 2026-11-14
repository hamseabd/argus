"""Handling webhook deliveries from the payment provider."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Delivery:
    id: str
    event: str


def handle(delivery: Delivery) -> str:
    """Process one delivery and say what was done with it."""
    return f"processed {delivery.event}"
