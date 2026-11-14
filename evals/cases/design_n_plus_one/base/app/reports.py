"""Reports over orders."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Order:
    id: int
    customer_id: int
    total_cents: int
